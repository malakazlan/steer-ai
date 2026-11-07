"""The `value` subgoal condition (issue #36; packet field-value-2026-11-06, schema gate REVISE 7;
hunter FV-02/05/06/07/08/38/45/48): a field holds exactly the text a step typed."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

import pytest

from steerai.action.physical import TEXT_ROLES
from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.verify.predicates import FIELD_ROLES, FieldValue
from steerai.verify.t1 import Verdict, evaluate, redact
from tests.orchestrator.test_loop import Harness, _type
from tests.verify.fakes import (
    BASE_ROWS,
    COMPOSER,
    COMPOSER_AREA,
    FakeHandle,
    FakeLive,
    Row,
    build,
)

SENTINEL = "sentinel-field-text-5b1c"


def test_the_field_roles_are_the_text_roles() -> None:
    """Gate item 3: on any other role the read returns the element's own name (FV-02)."""
    assert FIELD_ROLES == TEXT_ROLES


@pytest.mark.parametrize("role", ["Text", "Button", "Hyperlink", ""])
def test_a_non_field_role_is_refused(role: str) -> None:
    with pytest.raises(ValueError, match="role"):
        FieldValue(role=role, name_pattern="Text Editor", text="hello")


@pytest.mark.parametrize("text", ["", "   ", "\t"])
def test_empty_text_is_refused(text: str) -> None:
    """Gate item 2: an unreadable value reads as "" and would hold (FV-06)."""
    with pytest.raises(ValueError, match="text"):
        FieldValue(role="Document", name_pattern="Text Editor", text=text)


def test_text_over_the_type_cap_is_refused() -> None:
    """FV-38: a condition no `type` step can reach."""
    with pytest.raises(ValueError, match="text"):
        FieldValue(role="Edit", name_pattern="Search", text="x" * 501)


def test_redaction_keeps_the_role_only() -> None:
    """Gate item 6 / FV-08."""
    spec = redact(FieldValue(role="Document", name_pattern=SENTINEL, text=SENTINEL))
    assert spec == {"kind": "FieldValue", "role": "Document"}


# ---- the plan file ------------------------------------------------------------------------------


def _doc(condition: dict[str, Any], *, expect: dict[str, Any] | None = None) -> str:
    step: dict[str, Any] = {
        "id": "s1",
        "subgoal": "g1",
        "verb": "type",
        "target": {"role": "Document", "name": "Text Editor"},
        "text": "hello",
        "risk": "R1",
    }
    if expect is not None:
        step["expect"] = expect
    return json.dumps(
        {
            "goal": "type hello in notepad",
            "subgoals": [{"id": "g1", "desc": "hello is in notepad", "condition": condition}],
            "steps": [step],
        }
    )


VALUE = {"value": {"role": "Document", "name": "Text Editor", "text": "hello"}}


def test_a_value_condition_round_trips() -> None:
    plan = plan_from_json(_doc(VALUE))
    assert plan.subgoals[0].condition == FieldValue(
        role="Document", name_pattern="Text Editor", text="hello"
    )
    assert plan_from_json(plan_to_json(plan)).subgoals[0].condition == plan.subgoals[0].condition


@pytest.mark.parametrize(
    "expect",
    [
        VALUE,
        {"all": [{"title": {"pattern": "x"}}, VALUE]},
        {"any_of": [{"title": {"pattern": "x"}}, {"all": [VALUE]}]},
    ],
)
def test_value_is_never_a_step_expect_at_any_depth(expect: dict[str, Any]) -> None:
    """Gate item 5 / FV-07: presence with no before value cannot prove a step's effect."""
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc({"title": {"pattern": "x"}}, expect=expect))
    assert caught.value.path.startswith("$.steps[0].expect")
    assert caught.value.path.endswith(".value")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"role": "Text", "name": "x", "text": "hello"}, "role"),
        ({"role": "Document", "name": "Text Editor", "text": ""}, "text"),
        ({"role": "Document", "name": "Text Editor", "text": 3}, "text"),
        ({"role": "Document", "name": "", "text": "hello"}, "name"),
        ({"role": "Document", "name": "Text Editor", "text": "hello", "within": "x"}, None),
    ],
)
def test_a_bad_value_body_is_refused_at_its_path(body: dict[str, Any], field: str | None) -> None:
    """FV-45."""
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc({"value": body}))
    assert caught.value.path.startswith("$.subgoals[0].condition.value")
    if field is not None:
        assert field in caught.value.path or field in str(caught.value)


def test_the_live_kind_keeps_its_own_refusal() -> None:
    """FV-48: `value_equals` still needs a snapshot element id."""
    with pytest.raises(PlanError, match="snapshot element id"):
        plan_from_json(_doc({"value_equals": {"target_id": 1, "text": "x"}}))


# ---- plan rules (principal rulings on FV-01/03/21/23/25/42) -------------------------------------


def _plan_doc(steps: list[dict[str, Any]], condition: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "goal": "type hello in notepad",
            "subgoals": [
                {"id": "g1", "desc": "hello is in notepad", "condition": condition or VALUE}
            ],
            "steps": steps,
        }
    )


def _type_step(step_id: str = "s1", text: str = "hello", **kw: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": step_id,
        "subgoal": "g1",
        "verb": "type",
        "target": {"role": "Document", "name": kw.pop("name", "Text Editor")},
        "text": text,
        "risk": "R1",
    }
    step.update(kw)
    return step


def test_a_value_subgoal_matching_its_type_step_is_accepted() -> None:
    plan = plan_from_json(_plan_doc([_type_step(name="text editor")]))
    assert plan.steps[0].text == "hello"


def test_a_value_subgoal_without_steps_is_allowed() -> None:
    """Plan 1 lists every subgoal of the goal (5.3 [v2.1]); it cannot tick without its step."""
    plan = json.loads(_plan_doc([]))
    plan["subgoals"].append({"id": "g0", "desc": "d", "condition": {"title": {"pattern": "x"}}})
    plan["steps"] = [{**_type_step(), "subgoal": "g0"}]
    assert plan_from_json(json.dumps(plan)).subgoals[0].id == "g1"


@pytest.mark.parametrize(
    "step",
    [
        _type_step(text="Hello"),  # FV-25: exact text
        _type_step(name="Search"),  # another field
        {**_type_step(), "verb": "focus", "text": None},  # no type step at all
    ],
)
def test_a_value_subgoal_must_name_a_type_step_of_its_own(step: dict[str, Any]) -> None:
    """FV-03/42: the text was typed by this subgoal, never found or guessed."""
    if step.get("text") is None:
        step = {k: v for k, v in step.items() if k != "text"}
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan_doc([step]))
    assert caught.value.path == "$.subgoals[0].condition"


@pytest.mark.parametrize("verb", ["send", "submit"])
def test_a_value_subgoal_owns_no_send_or_submit(verb: str) -> None:
    """FV-01: the value holds after the type, before the effect the subgoal is for."""
    other = {
        "id": "s2",
        "subgoal": "g1",
        "verb": verb,
        "target": {"role": "Button", "name": "Send"},
        "risk": "R2",
        "params": {
            "text": "hello",
            "composer_role": "Document",
            "composer_name": "Text Editor",
            "scope": "Conversation",
        },
        "expect": {"appeared": {"role": "Text", "name": "hello"}},
    }
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan_doc([_type_step(), other]))
    assert caught.value.path == "$.subgoals[0].condition"


def test_a_value_subgoal_acting_in_two_windows_is_refused() -> None:
    """FV-23: checked in one window only."""
    steps = [
        {
            "id": "o1",
            "subgoal": "g0",
            "verb": "open",
            "target": {"role": "App", "name": "notepad"},
            "risk": "R1",
        },
        {**_type_step("s1"), "window": "opened:o1"},
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
    doc = json.loads(_plan_doc(steps))
    doc["subgoals"].insert(0, {"id": "g0", "desc": "d", "condition": {"title": {"pattern": "x"}}})
    with pytest.raises(PlanError) as caught:
        plan_from_json(json.dumps(doc))
    assert caught.value.path == "$.subgoals[1].condition"


# ---- the loop's tick rules (rulings FV-01, FV-20) ---------------------------------------------


def _value_plan(*steps: Any) -> Any:
    from steerai.orchestrator.contracts import Plan, Subgoal  # noqa: PLC0415

    return Plan(
        goal="type hello there",
        subgoals=(
            Subgoal(
                id="g1",
                desc="typed",
                condition=FieldValue(role="Edit", name_pattern="Message", text="hello there"),
            ),
        ),
        steps=steps,
    )


def test_a_value_subgoal_ticks_after_its_type_step() -> None:
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415

    h = Harness()
    result = h.session.run_task(_value_plan(_type()))
    assert result.status == "done", result
    assert result.ticked == {"g1"}


def test_a_value_subgoal_never_ticks_at_a_runs_start() -> None:
    """FV-01/03: a field already holding the text is the type step's business."""
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415

    h = Harness()
    h.world.composer_value = "hello there"
    result = h.session.run_task(_value_plan(_type()))
    assert len(result.results) == 1, "the type step ran: nothing ticked at start"


def test_a_value_subgoal_waits_for_its_last_step() -> None:
    """FV-01: the type held the value; the click after it is part of the subgoal."""
    from dataclasses import replace  # noqa: PLC0415

    from steerai.verify.predicates import TitleMatches  # noqa: PLC0415
    from tests.orchestrator.test_loop import Harness, _click, _type  # noqa: PLC0415

    h = Harness()
    after = replace(_click(step_id="c9", name="Chat options"), expect=TitleMatches(pattern="nope"))
    result = h.session.run_task(_value_plan(_type(), after))
    assert [r.step_id for r in result.results] == ["s2", "c9"], "the click was not skipped"
    assert "g1" not in result.ticked, "the click failed: the subgoal is not done"


def test_the_persons_input_during_a_step_withholds_the_tick() -> None:
    """FV-20: the person may have typed the text."""
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415
    from tests.orchestrator.test_takeover_loop import FakeMonitor, _wire  # noqa: PLC0415

    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)

    def person_types(_executor: Any, _action: Any) -> None:
        monitor.user_events += 1  # input counted during the step

    h.semantic.on_execute = person_types
    h.physical.on_execute = person_types
    result = h.session.run_task(_value_plan(_type()))
    assert "g1" not in result.ticked


def test_a_failed_live_read_never_ticks_and_never_escapes_the_task() -> None:
    """FV-16: the subgoal check after the type step."""
    from steerai.errors import ErrorCode, SteerError  # noqa: PLC0415
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415

    h = Harness()
    reads = {"n": 0}
    live = h.session._live
    assert live is not None
    real = live.value

    def value(handle: Any) -> str | None:
        reads["n"] += 1
        if reads["n"] > 1:  # the type's own check reads first; the subgoal check fails
            raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "live_read_failed")
        return real(handle)

    live.value = value  # type: ignore[method-assign]
    result = h.session.run_task(_value_plan(_type()))
    assert "g1" not in result.ticked


def test_a_failed_live_read_in_the_type_verify_is_a_failed_step_not_a_crash() -> None:
    """Ruling 4: the `type` step's own value check, the other caller of the live read."""
    from steerai.errors import ErrorCode, SteerError  # noqa: PLC0415
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415

    h = Harness()
    live = h.session._live
    assert live is not None

    def value(_handle: Any) -> str | None:
        raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "live_read_failed")

    live.value = value  # type: ignore[method-assign,assignment]
    result = h.session.run_step(_type())
    assert result.ok is False


# ---- tester gaps: evaluation on fake snapshots (gate items 1-4, FV-05/06/39) ------------------


@dataclass
class _CountingLive(FakeLive):
    """The verifier fakes' live reads, counting value reads: a field never resolved or a
    secure field is never read."""

    reads: int = 0

    def value(self, handle: Any) -> str | None:
        self.reads += 1
        return super().value(handle)


MESSAGE = FieldValue(role="Edit", name_pattern="Message", text="hello there")


def _judge(
    predicate: FieldValue,
    value: str | None,
    rows: list[Row] | None = None,
    *,
    before_rows: list[Row] | None = None,
    secure: bool = False,
) -> tuple[Verdict, _CountingLive]:
    live = _CountingLive(states={COMPOSER[3]: FakeHandle(value=value)})
    after = build(rows or BASE_ROWS, snapshot_id="after", taken_at=1.0)
    if secure:
        marked = [replace(e, secure=True) if e.id == COMPOSER[0] else e for e in after.elements]
        after = replace(after, elements=marked)
    before = build(before_rows or BASE_ROWS, snapshot_id="before")
    return evaluate(predicate, before, after, live=live), live


@pytest.mark.parametrize("name", ["Message", "message", "  MESSAGE "])
def test_a_field_holding_exactly_the_text_holds(name: str) -> None:
    """The field resolves by the target rule (role, folded name); the value compares exactly."""
    verdict, _ = _judge(replace(MESSAGE, name_pattern=name), "hello there")
    assert verdict.holds is True
    assert verdict.level == 3


def test_the_value_is_judged_in_the_after_snapshot() -> None:
    """Gate item 4: the field is resolved and read in the snapshot being judged."""
    without: list[Row] = [r for r in BASE_ROWS if r[0] != COMPOSER[0]]
    verdict, _ = _judge(MESSAGE, "hello there", before_rows=without)
    assert verdict.holds is True


def test_a_label_of_another_role_with_the_fields_name_is_not_the_field() -> None:
    """Gate item 3: only the field's role is resolved, a `Text` label does not make it two."""
    label = (20, "Text", "Message", (42, 60), COMPOSER_AREA[0])
    verdict, _ = _judge(MESSAGE, "hello there", [*BASE_ROWS, label])
    assert verdict.holds is True


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([r for r in BASE_ROWS if r[0] != COMPOSER[0]], "target_not_found"),
        ([*BASE_ROWS, (20, "Edit", "message", (42, 60), COMPOSER_AREA[0])], "ambiguous_target"),
    ],
)
def test_no_field_or_two_fields_never_hold_and_read_nothing(rows: list[Row], reason: str) -> None:
    """The `_resolve` rule: exactly one match, else the condition does not hold."""
    verdict, live = _judge(MESSAGE, "hello there", rows)
    assert verdict.holds is False
    assert verdict.evidence == {"reason": reason}
    assert live.reads == 0


def test_a_secure_field_is_never_read() -> None:
    """5.10 [v1.2]: a password box holding the exact text still does not hold."""
    verdict, live = _judge(MESSAGE, "hello there", secure=True)
    assert verdict.holds is False
    assert verdict.evidence == {"reason": "secure_target"}
    assert live.reads == 0
    assert verdict.state == "secure_target", "it settles: no polling to the deadline"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Hello there", "value_differs"),  # FV-05: case-sensitive
        ("hello  there", "value_differs"),  # no whitespace folding
        ("hello\nthere", "value_differs"),
        ("hello there ", "value_differs"),
        ("hello there and more", "value_differs"),  # full string, never contains
        ("", "value_empty"),  # FV-06
        (None, "value_empty"),
    ],
)
def test_a_field_with_other_text_does_not_hold(value: str | None, reason: str) -> None:
    verdict, _ = _judge(MESSAGE, value)
    assert verdict.holds is False
    assert verdict.evidence == {"reason": reason}


def test_newlines_compare_normalised() -> None:
    """Gate item 1: the `ValueEquals` rule normalises newlines and nothing else."""
    verdict, _ = _judge(replace(MESSAGE, text="hello\nthere"), "hello\r\nthere")
    assert verdict.holds is True


def test_a_verdict_never_carries_the_value_read() -> None:
    """Gate item 6: verdicts carry the reason token only, never the field's content."""
    verdict, _ = _judge(MESSAGE, SENTINEL)
    assert SENTINEL not in json.dumps(verdict.evidence)


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("x" * 500, True),
        ("\U0001f600" * 250, True),  # 500 UTF-16 units
        ("\U0001f600" * 250 + "x", False),  # 501 units, 251 code points
    ],
)
def test_the_text_cap_counts_utf16_units(text: str, accepted: bool) -> None:
    """FV-38 boundary: the limit is accepted, the limit plus one refused, in UTF-16 units."""
    if accepted:
        assert FieldValue(role="Edit", name_pattern="Search", text=text).text == text
    else:
        with pytest.raises(ValueError, match="UTF-16"):
            FieldValue(role="Edit", name_pattern="Search", text=text)


# ---- tester gaps: plan rules -----------------------------------------------------------------


def test_a_value_inside_all_is_a_condition_too() -> None:
    """A nested `value` in a subgoal condition is accepted and round-trips (gate item 5 is
    about `expect` only)."""
    condition = {"all": [{"title": {"pattern": "Notepad"}}, VALUE]}
    plan = plan_from_json(_plan_doc([_type_step()], condition))
    assert plan_from_json(plan_to_json(plan)).subgoals[0].condition == plan.subgoals[0].condition


@pytest.mark.parametrize(
    "condition",
    [
        {"all": [VALUE, {"value": {"role": "Document", "name": "Text Editor", "text": "bye"}}]},
        {
            "all": [
                VALUE,
                {
                    "any_of": [
                        {"title": {"pattern": "x"}},
                        {"value": {"role": "Edit", "name": "To", "text": "bye"}},
                    ]
                },
            ]
        },
    ],
)
def test_every_value_at_any_depth_names_a_type_step(condition: dict[str, Any]) -> None:
    """Ruling 2: each value part is checked, however deep, not only the first."""
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan_doc([_type_step()], condition))
    assert caught.value.path == "$.subgoals[0].condition"


def test_the_type_steps_role_must_be_the_values() -> None:
    """Ruling 2: the same role, not only the same name."""
    step = {**_type_step(), "target": {"role": "Edit", "name": "Text Editor"}}
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan_doc([step]))
    assert caught.value.path == "$.subgoals[0].condition"


def test_a_type_of_another_subgoal_does_not_count() -> None:
    """Ruling 2: the type step is the value subgoal's own."""
    doc = json.loads(
        _plan_doc(
            [
                {**_type_step("s1"), "subgoal": "g0"},
                {
                    "id": "s2",
                    "subgoal": "g1",
                    "verb": "click",
                    "target": {"role": "Button", "name": "OK"},
                    "risk": "R0",
                    "expect": {"title": {"pattern": "x"}},
                },
            ]
        )
    )
    doc["subgoals"].insert(0, {"id": "g0", "desc": "d", "condition": {"title": {"pattern": "x"}}})
    with pytest.raises(PlanError) as caught:
        plan_from_json(json.dumps(doc))
    assert caught.value.path == "$.subgoals[1].condition"


@pytest.mark.parametrize(
    "other",
    [
        {
            "id": "s2",
            "subgoal": "g1",
            "verb": "click",
            "target": {"role": "Button", "name": "OK"},
            "risk": "R2",
            "expect": {"appeared": {"role": "Text", "name": "hello"}},
        },
        {
            "id": "s2",
            "subgoal": "g1",
            "verb": "delete",
            "target": {"role": "Button", "name": "Delete"},
            "risk": "R3",
            "expect": {"disappeared": {"role": "Text", "name": "hello"}},
        },
    ],
)
def test_a_value_subgoal_owns_no_declared_r2_step_or_delete(other: dict[str, Any]) -> None:
    """Ruling 1: a declared R2/R3 step of any verb, and a delete."""
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan_doc([_type_step(), other]))
    assert caught.value.path == "$.subgoals[0].condition"


_OPEN = {
    "id": "o1",
    "subgoal": "g1",
    "verb": "open",
    "target": {"role": "App", "name": "notepad"},
    "risk": "R1",
}


def test_a_value_subgoal_may_open_the_window_it_types_in() -> None:
    """FV-23: an `open` acts in no window of its own; the type after it is in the new one."""
    plan = plan_from_json(_plan_doc([_OPEN, _type_step()]))
    assert [s.verb for s in plan.steps] == ["open", "type"]


def test_an_open_moves_the_later_steps_to_its_window() -> None:
    """FV-23: plan order, as `_plan_refs`: a step after an open, with no window, acts in it."""
    click = {
        "id": "s2",
        "subgoal": "g1",
        "verb": "click",
        "target": {"role": "Button", "name": "OK"},
        "risk": "R0",
        "window": "opened:o1",
        "expect": {"title": {"pattern": "x"}},
    }
    doc = json.loads(_plan_doc([{**_OPEN, "subgoal": "g0"}, _type_step(), click]))
    doc["subgoals"].insert(0, {"id": "g0", "desc": "d", "condition": {"title": {"pattern": "x"}}})
    assert len(plan_from_json(json.dumps(doc)).steps) == 3


# ---- tester gaps: the loop's tick rules ----------------------------------------------------------


@pytest.mark.parametrize(
    ("risk", "ready"), [("R0", True), ("R1", True), ("R2", False), ("R3", False)]
)
def test_a_value_subgoal_is_not_judged_after_a_run_time_r2_step(risk: str, ready: bool) -> None:
    """Ruling 1: the last step's run-time tier, not its declared one (like `renamed_at_risk`)."""
    from steerai.orchestrator.contracts import StepResult  # noqa: PLC0415
    from steerai.orchestrator.loop import _value_ready  # noqa: PLC0415
    from tests.orchestrator.test_loop import _type  # noqa: PLC0415

    step = _type()
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
    assert _value_ready(_value_plan(step), step, result, [result]) is ready


def test_the_persons_input_during_an_earlier_step_withholds_the_tick() -> None:
    """FV-20: input during any step of the subgoal, not only the last one."""
    from steerai.verify.predicates import Appeared, Within  # noqa: PLC0415
    from tests.orchestrator.test_loop import Harness, _click, _type  # noqa: PLC0415
    from tests.orchestrator.test_takeover_loop import FakeMonitor, _wire  # noqa: PLC0415

    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)

    def act(_executor: Any, action: Any) -> None:
        if action.verb == "type":
            monitor.user_events += 1  # the person types during the type step only
        else:
            h.world.add_bubble("menu shown")  # the click's own effect

    h.semantic.on_execute = act
    h.physical.on_execute = act
    shown = Appeared(role="Text", name_pattern="menu shown", within=Within(landmark="Conversation"))
    result = h.session.run_task(_value_plan(_type(), _click(step_id="c9", expect=shown)))
    assert [r.step_id for r in result.results] == ["s2", "c9"]
    assert result.results[-1].ok, "the last step itself ran ok, with no input"
    assert "g1" not in result.ticked


def test_a_done_already_r2_step_never_ticks_a_value_subgoal() -> None:
    """Ruling 1 on the `done_already` path: an earlier verified R2 step proves its own effect,
    never that the field holds the text."""
    from tests.orchestrator.test_loop import Harness, _send, _type  # noqa: PLC0415

    h = Harness()
    assert h.session.run_step(_type()).ok
    assert h.session.run_step(_send()).ok
    result = h.session.run_task(_value_plan(_send()))
    assert result.results[0].gate == "done_already"
    assert "g1" not in result.ticked


def test_a_value_subgoal_never_ticks_at_its_open() -> None:
    """Ruling 1 / FV-03/13: a window that opens already holding the text (session restore)
    is not the type's doing; the open is never the subgoal's last step."""
    from tests.orchestrator.test_loop import _type  # noqa: PLC0415
    from tests.orchestrator.test_open_loop import Rig, _open  # noqa: PLC0415

    rig = Rig()
    rig.world.composer_value = "hello there"
    session = rig.build()
    plan = _value_plan(_open(), _type())
    plan = replace(plan, goal="search YouTube for drake latest video, type hello there")
    result = session.run_task(plan)
    assert [r.step_id for r in result.results][:1] == ["o1"]
    assert "s2" in [r.step_id for r in result.results], (result.status, result.ticked)


def test_a_person_target_type_is_not_the_values_match() -> None:
    """Principal carry-over: a person target or an automation id resolves differently."""
    for extra in ({"match": "person", "name": "Text"}, {"automation_id": "editor"}):
        step = _type_step()
        step["target"] = {**step["target"], **extra}
        with pytest.raises(PlanError):
            plan_from_json(_plan_doc([step]))


# ---- the value condition takes the step's text-role alias (alias gate re-review, option A) ----


def _as_role(role: str) -> list[Row]:
    return [(r[0], role, *r[2:]) if r[0] == COMPOSER[0] else r for r in BASE_ROWS]


def test_a_value_condition_naming_edit_holds_on_the_same_named_document() -> None:
    """Live alias-4/5 (2026-11-07): the plan named Edit "Text Editor", Notepad shows a Document;
    the steps aliased and the value condition never held."""
    verdict, _ = _judge(MESSAGE, "hello there", _as_role("Document"))
    assert verdict.holds is True
    assert verdict.evidence == {"alias": "Document"}


def test_the_exact_role_is_judged_before_an_alias() -> None:
    other = (21, "Document", "Message", (42, 61), COMPOSER_AREA[0])
    verdict, _ = _judge(MESSAGE, "hello there", [*BASE_ROWS, other])
    assert verdict.holds is True
    assert "alias" not in verdict.evidence


def test_two_alias_fields_never_hold() -> None:
    twin = (21, "ComboBox", "Message", (42, 61), COMPOSER_AREA[0])
    verdict, live = _judge(MESSAGE, "hello there", [*_as_role("Document"), twin])
    assert verdict.holds is False
    assert verdict.evidence == {"reason": "ambiguous_target"}
    assert live.reads == 0


def test_a_secure_alias_field_is_never_a_candidate() -> None:
    """Issue #77: the step alias drops secure fields before counting; so does the condition."""
    verdict, live = _judge(MESSAGE, "hello there", _as_role("Document"), secure=True)
    assert verdict.holds is False
    assert verdict.evidence == {"reason": "target_not_found"}
    assert live.reads == 0


def test_a_secure_twin_leaves_the_normal_alias_field_as_the_one() -> None:
    """Issue #77: a secure and a normal field of alias roles with the same name: the step
    resolves the normal one, and the condition judges the same one."""
    twin = (21, "ComboBox", "Message", (42, 61), COMPOSER_AREA[0])
    rows = [*_as_role("Document"), twin]
    live = _CountingLive(states={COMPOSER[3]: FakeHandle(value="hello there")})
    after = build(rows, snapshot_id="after", taken_at=1.0)
    secured = [replace(e, secure=True) if e.id == twin[0] else e for e in after.elements]
    after = replace(after, elements=secured)
    verdict = evaluate(MESSAGE, build(BASE_ROWS, snapshot_id="before"), after, live=live)
    assert verdict.holds is True
    assert verdict.evidence == {"alias": "Document"}
    assert live.reads == 1, "only the normal field was read; the secure twin never"


def test_a_plan_naming_edit_ticks_when_the_field_is_a_document() -> None:
    """Alias gate option A end to end (live alias-4/5): the type step and the value condition
    both name Edit; the field is a Document; the steps alias and the subgoal ticks."""
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415

    h = Harness()
    h.world.rows = _as_role("Document")
    result = h.session.run_task(_value_plan(_type()))
    assert result.status == "done", result
    assert result.ticked == {"g1"}
    assert result.results[0].evidence["alias"] == "Document"


def test_an_aliased_value_never_ticks_at_a_runs_start_either() -> None:
    """Parity with the exact field: a value is the type step's business (FV-01/03)."""
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415

    h = Harness()
    h.world.rows = _as_role("Document")
    h.world.composer_value = "hello there"
    result = h.session.run_task(_value_plan(_type()))
    assert len(result.results) == 1, "the type step ran: nothing ticked at start"


def test_an_aliased_condition_tick_is_traced_with_its_role() -> None:
    """Issue #76: the tick record carries the verdict's alias (a role name, never content)."""
    h = Harness()
    h.world.rows = _as_role("Document")
    assert h.session.run_task(_value_plan(_type())).ticked == {"g1"}
    ticks = [json.loads(line) for line in h.trace.lines if '"kind": "tick"' in line]
    assert ticks == [
        {
            "kind": "tick",
            "task_id": h.session.task_id,
            "subgoal": "g1",
            "by": "condition",
            "alias": "Document",
        }
    ]


def test_an_exact_condition_tick_carries_no_alias() -> None:
    h = Harness()
    assert h.session.run_task(_value_plan(_type())).ticked == {"g1"}
    ticks = [json.loads(line) for line in h.trace.lines if '"kind": "tick"' in line]
    assert [set(t) for t in ticks] == [{"kind", "task_id", "subgoal", "by"}]


def test_an_aliased_value_inside_an_all_keeps_its_alias_on_the_tick() -> None:
    """Principal review of #79: `all` rebuilt its evidence and dropped the alias."""
    from steerai.orchestrator.contracts import Plan, Subgoal  # noqa: PLC0415
    from steerai.verify.predicates import All  # noqa: PLC0415

    h = Harness()
    h.world.rows = _as_role("Document")
    condition = All(parts=(MESSAGE, replace(MESSAGE, name_pattern="message")))  # one role
    plan = Plan(
        goal="type hello there",
        subgoals=(Subgoal(id="g1", desc="typed", condition=condition),),
        steps=(_type(),),
    )
    assert h.session.run_task(plan).ticked == {"g1"}
    ticks = [json.loads(line) for line in h.trace.lines if '"kind": "tick"' in line]
    assert [t.get("alias") for t in ticks] == ["Document"]


def test_a_tick_never_records_content_as_an_alias() -> None:
    """5.14 redaction: only a field-role token is written, whatever reaches the record."""
    h = Harness()
    h.session._trace_tick("g1", "condition", alias="hello there")
    (record,) = [json.loads(line) for line in h.trace.lines if '"kind": "tick"' in line]
    assert "alias" not in record
    assert "hello" not in json.dumps(record)


def test_an_all_whose_parts_alias_to_two_roles_records_no_alias() -> None:
    """[v2.21]: several distinct alias roles give no alias (never a set-order pick)."""
    from steerai.verify.predicates import All  # noqa: PLC0415

    combo = (22, "ComboBox", "Subject", (42, 62), COMPOSER_AREA[0])
    rows = [*_as_role("Document"), combo]
    subject = FieldValue(role="Edit", name_pattern="Subject", text="hello there")
    live = _CountingLive(
        states={
            COMPOSER[3]: FakeHandle(value="hello there"),
            (42, 62): FakeHandle(value="hello there"),
        }
    )
    after = build(rows, snapshot_id="after", taken_at=1.0)
    verdict = evaluate(
        All(parts=(MESSAGE, subject)), build(BASE_ROWS, snapshot_id="before"), after, live=live
    )
    assert verdict.holds is True
    assert "alias" not in verdict.evidence
