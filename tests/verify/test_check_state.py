"""The `state` subgoal condition (design W2-4): the one CheckBox of this name reads on or off.
A condition only, never a step expect (a check/uncheck verb owns its postcondition), proven by a
check or uncheck step of the same subgoal, and judged only after that subgoal's last step."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import Plan, Step, StepResult, Subgoal, Target
from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.orchestrator.trace import ListTrace
from steerai.verify.predicates import (
    All,
    AnyOf,
    CheckState,
    FieldValue,
    Predicate,
    TitleMatches,
    WindowExists,
    check_states,
)
from steerai.verify.t1 import evaluate, redact, verify
from tests.orchestrator.test_state_verbs_loop import ALERTS, Boxes, _flips, _harness
from tests.verify.fakes import BASE_ROWS, FakeHandle, FakeLive, Row, build

SENTINEL = "sentinel-box-name-7e2a"
ALERTS_BOX: Row = (20, "CheckBox", "Email alerts", (42, 60), 0)
TERMS_BOX: Row = (21, "CheckBox", "I accept the terms", (42, 61), 0)
ALERTS_TEXT: Row = (22, "Text", "Email alerts", (42, 62), 0)


def _on(name: str = "Email alerts", wanted: Any = "on") -> CheckState:
    return CheckState(role="CheckBox", name_pattern=name, wanted=wanted)


# ---- the predicate -------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["MenuItem", "ToggleButton", "Edit", ""])
def test_a_non_state_role_is_refused(role: str) -> None:
    with pytest.raises(ValueError, match="role"):
        CheckState(role=role, name_pattern="Email alerts", wanted="on")


def test_a_button_is_never_on_or_off() -> None:
    """W5: a Button is a state role (expanded / collapsed), so the disagreement names wanted."""
    with pytest.raises(ValueError, match="wanted for a Button must be expanded or collapsed"):
        CheckState(role="Button", name_pattern="Email alerts", wanted="on")


@pytest.mark.parametrize("name", ["", "   ", "\t"])
def test_an_empty_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        CheckState(role="CheckBox", name_pattern=name, wanted="on")


@pytest.mark.parametrize("wanted", ["selected", "ON", "", "true"])
def test_wanted_is_on_or_off(wanted: Any) -> None:
    with pytest.raises(ValueError, match="wanted"):
        CheckState(role="CheckBox", name_pattern="Email alerts", wanted=wanted)


def test_the_role_is_the_state_verbs_role() -> None:
    """Pinned: the predicates module cannot import the contracts (they import it)."""
    from steerai.orchestrator.contracts import STATE_VERBS, state_roles  # noqa: PLC0415
    from steerai.verify.predicates import STATE_ROLES  # noqa: PLC0415
    from steerai.verify.strength import STATE_WANTED  # noqa: PLC0415

    assert state_roles("check") == state_roles("uncheck") == STATE_ROLES["on"] == STATE_ROLES["off"]
    assert {state_roles(verb) for verb in STATE_VERBS} == set(STATE_ROLES.values())
    assert set(STATE_WANTED.values()) == set(STATE_ROLES)


def test_the_name_stays_out_of_repr() -> None:
    assert SENTINEL not in repr(_on(SENTINEL))


def test_check_states_are_found_at_any_depth() -> None:
    first, second = _on(), _on("I accept the terms", "off")
    title = TitleMatches(pattern="x")
    nested = AnyOf(parts=(title, All(parts=(first, second))))
    assert check_states(nested) == [first, second]
    assert check_states(first) == [first]
    assert check_states(title) == []


# ---- the plan file -------------------------------------------------------------------------------


STATE = {"state": {"role": "CheckBox", "name": "Email alerts", "wanted": "on"}}


def _check_step(
    step_id: str = "s1", verb: str = "check", name: str = "Email alerts", **kw: Any
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": step_id,
        "subgoal": "g1",
        "verb": verb,
        "target": {"role": "CheckBox", "name": name},
        "risk": "R1",
    }
    step.update(kw)
    return step


def _doc(steps: list[dict[str, Any]] | None = None, condition: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "goal": "turn on email alerts",
            "subgoals": [{"id": "g1", "desc": "alerts on", "condition": condition or STATE}],
            "steps": [_check_step()] if steps is None else steps,
        }
    )


def test_a_state_condition_parses_and_round_trips() -> None:
    plan = plan_from_json(_doc())
    assert plan.subgoals[0].condition == _on()
    text = plan_to_json(plan)
    assert json.loads(text)["subgoals"][0]["condition"] == STATE
    assert plan_from_json(text) == plan


def test_an_off_state_round_trips_inside_all() -> None:
    off = {"state": {"role": "CheckBox", "name": "I accept the terms", "wanted": "off"}}
    condition = {"all": [STATE, off]}
    steps = [_check_step(), _check_step("s2", "uncheck", "I accept the terms")]
    plan = plan_from_json(_doc(steps, condition))
    assert plan.subgoals[0].condition == All(parts=(_on(), _on("I accept the terms", "off")))
    assert json.loads(plan_to_json(plan))["subgoals"][0]["condition"] == condition


@pytest.mark.parametrize(
    "expect",
    [STATE, {"all": [{"title": {"pattern": "x"}}, STATE]}, {"any_of": [{"all": [STATE]}]}],
)
def test_state_is_never_a_step_expect_at_any_depth(expect: dict[str, Any]) -> None:
    click = {
        "id": "s1",
        "subgoal": "g1",
        "verb": "click",
        "target": {"role": "Button", "name": "Save"},
        "risk": "R1",
        "expect": expect,
    }
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([click], {"title": {"pattern": "x"}}))
    assert caught.value.path.startswith("$.steps[0].expect")
    assert caught.value.path.endswith(".state")
    assert "check, uncheck, select, expand and collapse own their postcondition" in str(
        caught.value
    )


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"role": "MenuItem", "name": "Email alerts", "wanted": "on"}, "role"),
        ({"role": "Button", "name": "Email alerts", "wanted": "on"}, "wanted"),
        ({"role": "CheckBox", "name": "", "wanted": "on"}, "name"),
        ({"role": "CheckBox", "name": "Email alerts", "wanted": "selected"}, "wanted"),
        ({"role": "CheckBox", "name": "Email alerts"}, "wanted"),
        ({"role": "CheckBox", "name": "Email alerts", "wanted": True}, "wanted"),
        ({"role": "CheckBox", "name": "Email alerts", "wanted": "on", "target_id": 3}, None),
    ],
)
def test_a_bad_state_body_is_refused_at_its_path(body: dict[str, Any], field: str | None) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc(condition={"state": body}))
    assert caught.value.path.startswith("$.subgoals[0].condition.state")
    if field is not None:
        assert field in caught.value.path or field in str(caught.value)


def test_a_state_subgoal_without_steps_is_allowed() -> None:
    """Plan 1 lists every subgoal of the goal (5.3 [v2.1]); it cannot tick without its step."""
    doc = json.loads(_doc([]))
    doc["subgoals"].append({"id": "g0", "desc": "d", "condition": {"title": {"pattern": "x"}}})
    doc["steps"] = [{**_check_step(), "subgoal": "g0"}]
    assert plan_from_json(json.dumps(doc)).subgoals[0].condition == _on()


@pytest.mark.parametrize(
    "step",
    [
        _check_step(verb="uncheck"),  # the wrong verb for on
        _check_step(name="SMS alerts"),  # another box
        _check_step(target={"role": "CheckBox", "name": "Email alerts", "automation_id": "a1"}),
        {
            "id": "s1",
            "subgoal": "g1",
            "verb": "click",
            "target": {"role": "CheckBox", "name": "Email alerts"},
            "risk": "R1",
            "expect": {"title": {"pattern": "x"}},
        },  # a click is no proof of the state
    ],
)
def test_a_state_subgoal_must_name_its_check_step(step: dict[str, Any]) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([step]))
    assert caught.value.path == "$.subgoals[0].condition"


def test_the_box_name_matches_case_and_whitespace_folded() -> None:
    plan = plan_from_json(_doc([_check_step(name="  email   ALERTS ")]))
    assert plan.steps[0].verb == "check"


def test_a_check_step_of_another_subgoal_is_no_proof() -> None:
    doc = json.loads(_doc([]))
    doc["subgoals"].append({"id": "g2", "desc": "d", "condition": {"title": {"pattern": "x"}}})
    doc["steps"] = [{**_check_step(), "subgoal": "g2"}, {**_check_step("s2"), "verb": "uncheck"}]
    doc["steps"][1]["target"]["name"] = "SMS alerts"
    with pytest.raises(PlanError) as caught:
        plan_from_json(json.dumps(doc))
    assert caught.value.path == "$.subgoals[0].condition"


@pytest.mark.parametrize("verb", ["send", "submit", "delete"])
def test_a_state_subgoal_owns_no_send_submit_or_delete(verb: str) -> None:
    other = {
        "id": "s2",
        "subgoal": "g1",
        "verb": verb,
        "target": {"role": "Button", "name": "Save"},
        "risk": "R3" if verb == "delete" else "R2",
        "params": {"text": "x", "scope": "Settings"},
        "expect": {"disappeared": {"role": "Text", "name": "x"}},
    }
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([_check_step(), other]))
    assert caught.value.path == "$.subgoals[0].condition"
    assert "send, submit or delete" in str(caught.value)


def test_a_state_subgoal_acting_in_two_windows_is_refused() -> None:
    steps = [
        {
            "id": "o1",
            "subgoal": "g0",
            "verb": "open",
            "target": {"role": "App", "name": "notepad"},
            "risk": "R1",
        },
        {**_check_step("s1"), "window": "opened:o1"},
        {
            "id": "s2",
            "subgoal": "g1",
            "verb": "click",
            "target": {"role": "Button", "name": "OK"},
            "risk": "R0",
            "window": "start",
            "expect": {"title": {"pattern": "x"}},
        },
    ]
    doc = json.loads(_doc(steps))
    doc["subgoals"].insert(0, {"id": "g0", "desc": "d", "condition": {"title": {"pattern": "x"}}})
    with pytest.raises(PlanError) as caught:
        plan_from_json(json.dumps(doc))
    assert caught.value.path == "$.subgoals[1].condition"
    assert "one window" in str(caught.value)


@pytest.mark.parametrize("risk", ["R2", "R3"])
def test_a_state_subgoal_may_hold_an_r2_or_r3_check(risk: str) -> None:
    """Unlike `value`: a consent box is R2 by design (the gate's consent class)."""
    terms = {"state": {"role": "CheckBox", "name": "I accept the terms", "wanted": "on"}}
    plan = plan_from_json(_doc([_check_step(name="I accept the terms", risk=risk)], terms))
    assert plan.steps[0].risk == risk


# ---- T1: one snapshot ----------------------------------------------------------------------------


def _snapshot(*extra: Row) -> Any:
    return build([*BASE_ROWS, *extra], snapshot_id="judged")


def _live(*toggles: tuple[Row, int | None]) -> FakeLive:
    return FakeLive(states={row[3]: FakeHandle(toggle=value) for row, value in toggles})


def _judge(predicate: Predicate, live: Any, *rows: Row) -> Any:
    snapshot = _snapshot(*rows)
    return evaluate(predicate, snapshot, snapshot, live=live)


@pytest.mark.parametrize(("wanted", "toggle"), [("on", 1), ("off", 0)])
def test_the_condition_holds_when_the_box_reads_the_wanted_state(wanted: str, toggle: int) -> None:
    verdict = _judge(_on(wanted=wanted), _live((ALERTS_BOX, toggle)), ALERTS_BOX, ALERTS_TEXT)
    assert verdict.holds is True


@pytest.mark.parametrize(("wanted", "toggle"), [("on", 0), ("off", 1), ("on", 2), ("off", 2)])
def test_another_state_differs_and_settles(wanted: str, toggle: int) -> None:
    """A condition is judged on one snapshot, nothing in flight: unlike the step's `State`,
    whose `state_differs` never settles (WV-03)."""
    verdict = _judge(_on(wanted=wanted), _live((ALERTS_BOX, toggle)), ALERTS_BOX)
    assert verdict.holds is False
    assert verdict.evidence == {"reason": "state_differs"}
    assert verdict.state == "state_differs"


def test_the_box_matches_case_and_whitespace_folded() -> None:
    verdict = _judge(_on("  EMAIL   alerts "), _live((ALERTS_BOX, 1)), ALERTS_BOX)
    assert verdict.holds is True


def test_a_longer_name_is_not_the_box() -> None:
    longer: Row = (23, "CheckBox", "Email alerts weekly", (42, 63), 0)
    verdict = _judge(_on(), _live((longer, 1)), longer)
    assert verdict.evidence == {"reason": "target_not_found"}
    assert verdict.state == "target_not_found"


def test_no_box_of_that_name_is_not_found() -> None:
    """A Text of the same name is never the box."""
    verdict = _judge(_on(), _live(), ALERTS_TEXT)
    assert verdict.holds is False
    assert verdict.evidence == {"reason": "target_not_found"}
    assert verdict.state == "target_not_found"


def test_twin_boxes_are_ambiguous() -> None:
    twin: Row = (24, "CheckBox", "email alerts", (42, 64), 0)
    verdict = _judge(_on(), _live((ALERTS_BOX, 1), (twin, 1)), ALERTS_BOX, twin)
    assert verdict.holds is False
    assert verdict.evidence == {"reason": "ambiguous_target"}
    assert verdict.state == "ambiguous_target"


def test_a_box_without_a_toggle_pattern_settles() -> None:
    verdict = _judge(_on(), _live((ALERTS_BOX, None)), ALERTS_BOX)
    assert verdict.holds is False
    assert verdict.evidence == {"reason": "no_pattern"}
    assert verdict.state == "no_pattern"


def test_the_box_is_read_through_the_judged_snapshots_handle() -> None:
    live = _live((ALERTS_BOX, 0), (TERMS_BOX, 1))
    assert _judge(_on("I accept the terms"), live, ALERTS_BOX, TERMS_BOX).holds is True
    assert _judge(_on(), live, ALERTS_BOX, TERMS_BOX).holds is False


class _FailingLive(FakeLive):
    def toggle_state(self, _handle: Any) -> int | None:
        raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "live_read_failed")


def test_a_failed_live_read_raises_like_a_value_read() -> None:
    """The FieldValue rule: the read error is the caller's (the loop's verdict is None)."""
    with pytest.raises(SteerError):
        _judge(_on(), _FailingLive(), ALERTS_BOX)


def test_a_failed_live_read_is_no_verdict_in_verify() -> None:
    snapshot = _snapshot(ALERTS_BOX)
    verdict = verify(_on(), snapshot, snapshot, lambda **_kw: snapshot, live=_FailingLive())
    assert verdict.holds is False
    assert verdict.code is ErrorCode.PERCEPTION_TIMEOUT
    assert verdict.evidence["reason"] == "evaluate_failed"


def test_a_settled_difference_fails_without_waiting_for_the_deadline() -> None:
    snapshot = _snapshot(ALERTS_BOX)
    clock = {"t": 0.0}
    verdict = verify(
        _on(),
        snapshot,
        snapshot,
        lambda **_kw: snapshot,
        live=_live((ALERTS_BOX, 0)),
        monotonic=lambda: clock["t"],
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
    )
    assert verdict.code is ErrorCode.VERIFY_FAILED
    assert verdict.polls == 1


def test_redaction_keeps_the_role_and_wanted_only() -> None:
    spec = redact(_on(SENTINEL, "off"))
    assert spec == {"kind": "CheckState", "role": "CheckBox", "wanted": "off"}
    assert SENTINEL not in json.dumps(redact(All(parts=(_on(SENTINEL), TitleMatches("x")))))


# ---- the loop's tick rules -----------------------------------------------------------------------


def _check(name: str = "Email alerts", verb: str = "check", step_id: str = "c1") -> Step:
    return Step(id=step_id, subgoal="g1", verb=verb, target=Target(role="CheckBox", name=name))


def _state_plan(*steps: Step, condition: Predicate | None = None) -> Plan:
    return Plan(
        goal="turn on email alerts",
        subgoals=(Subgoal(id="g1", desc="alerts on", condition=condition or _on()),),
        steps=steps,
    )


def _ticks(trace: ListTrace) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in trace.lines]
    return [r for r in records if r.get("kind") == "tick"]


def test_a_state_subgoal_ticks_after_its_check_step() -> None:
    h, boxes = _harness()
    h.semantic.on_execute = _flips(boxes)
    result = h.session.run_task(_state_plan(_check()))
    assert result.status == "done", result
    assert result.ticked == {"g1"}
    assert [(t["subgoal"], t["by"]) for t in _ticks(h.trace)] == [("g1", "condition")]


def test_an_uncheck_ticks_an_off_subgoal() -> None:
    h, boxes = _harness()
    boxes.state[ALERTS[3]] = 1
    h.semantic.on_execute = _flips(boxes)
    plan = _state_plan(_check(verb="uncheck"), condition=_on(wanted="off"))
    result = h.session.run_task(plan)
    assert result.status == "done", result
    assert boxes.state[ALERTS[3]] == 0


def test_a_state_subgoal_never_ticks_at_a_runs_start() -> None:
    """The box already on is the check step's business: it runs and proves it."""
    h, boxes = _harness()
    boxes.state[ALERTS[3]] = 1
    h.semantic.on_execute = _flips(boxes)
    result = h.session.run_task(_state_plan(_check()))
    assert [r.step_id for r in result.results] == ["c1"], "nothing ticked at start"
    assert all(t["by"] != "start" for t in _ticks(h.trace))
    assert result.ticked == {"g1"}


def test_a_state_subgoal_waits_for_its_last_step() -> None:
    """The first check holds its part; the subgoal is judged once, after the consent box."""
    h, boxes = _harness()
    h.semantic.on_execute = _flips(boxes)
    both = All(parts=(_on(), _on("I accept the terms")))
    plan = _state_plan(_check(), _check("I accept the terms", step_id="c2"), condition=both)
    result = h.session.run_task(plan)
    assert [r.step_id for r in result.results] == ["c1", "c2"]
    assert result.status == "done", result
    assert [t["subgoal"] for t in _ticks(h.trace)] == ["g1"]


def test_an_r2_consent_check_ticks_its_state_subgoal() -> None:
    """Unlike `value`: the consent box asks (R2) and its subgoal still ticks after it."""
    h, boxes = _harness()
    h.semantic.on_execute = _flips(boxes)
    plan = _state_plan(_check("I accept the terms"), condition=_on("I accept the terms"))
    result = h.session.run_task(plan)
    assert len(h.confirmer.asked) == 1
    assert h.confirmer.asked[0].risk == "R2"
    assert result.ticked == {"g1"}


@pytest.mark.parametrize(
    ("condition", "risk", "ready"),
    [
        (CheckState(role="CheckBox", name_pattern="I accept the terms", wanted="on"), "R2", True),
        (CheckState(role="CheckBox", name_pattern="I accept the terms", wanted="on"), "R3", True),
        (FieldValue(role="Edit", name_pattern="Message", text="hi"), "R2", False),
    ],
)
def test_only_a_value_subgoal_refuses_a_run_time_r2_step(
    condition: Predicate, risk: str, ready: bool
) -> None:
    from steerai.orchestrator.loop import _value_ready  # noqa: PLC0415

    step = _check("I accept the terms")
    plan = _state_plan(step, condition=condition)
    result = StepResult(
        step_id=step.id,
        ok=True,
        gate="approve",
        action=None,
        verify_code=None,
        label_kind="t1",
        latency_ms={},
        evidence={"risk": risk},
    )
    assert _value_ready(plan, step, result, [result]) is ready


def test_the_persons_input_during_the_check_withholds_the_tick() -> None:
    """The person may have ticked the box."""
    from tests.orchestrator.test_takeover_loop import FakeMonitor, _wire  # noqa: PLC0415

    h, boxes = _harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    h.session._live = boxes
    flip = _flips(boxes)

    def person_clicks(executor: Any, action: Any) -> None:
        monitor.user_events += 1
        flip(executor, action)

    h.semantic.on_execute = person_clicks
    result = h.session.run_task(_state_plan(_check()))
    assert result.results[0].ok is True
    assert "g1" not in result.ticked


class _CountingBoxes(Boxes):
    fail_at: int | None = None

    def toggle_state(self, handle: Any) -> int | None:
        if self.fail_at is not None and self.reads + 1 >= self.fail_at:
            self.reads += 1
            raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "live_read_failed")
        return super().toggle_state(handle)


def _counting(fail_at: int | None) -> tuple[Any, _CountingBoxes]:
    h, plain = _harness()
    boxes = _CountingBoxes(h.world, state=dict(plain.state))
    boxes.fail_at = fail_at
    h.session._live = boxes
    h.semantic.on_execute = _flips(boxes)
    return h, boxes


def test_a_failed_live_read_of_the_condition_never_ticks_and_never_escapes() -> None:
    h, boxes = _counting(None)
    assert h.session.run_task(_state_plan(_check())).ticked == {"g1"}
    # the subgoal's own read; the re-read before done (W4-04) is the run's last
    last = boxes.reads - 1
    h, _ = _counting(last)
    result = h.session.run_task(_state_plan(_check()))
    assert result.results[0].ok is True, "the step's own check read before the failure"
    assert "g1" not in result.ticked


def test_an_open_never_ticks_a_state_subgoal() -> None:
    """The title part would hold in the new window: only the state guard keeps it unticked."""
    from tests.orchestrator.test_open_loop import NOTEPAD_TITLE  # noqa: PLC0415
    from tests.orchestrator.test_open_subgoal import _notepad, _rig  # noqa: PLC0415

    trace = ListTrace()
    condition = AnyOf(parts=(WindowExists(name_pattern=NOTEPAD_TITLE), _on()))
    plan = Plan(
        goal="open notepad",
        subgoals=(Subgoal(id="g1", desc="alerts on", condition=condition),),
        steps=(_notepad(),),
    )
    result = _rig(trace).build().run_task(plan)
    assert result.results[0].ok is True
    assert result.ticked == set()
    assert _ticks(trace) == []
