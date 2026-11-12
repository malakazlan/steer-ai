"""The `state` condition with wanted `expanded` or `collapsed` (W5): a Button only, proven by an
expand or collapse step of the same subgoal on that exact element, and judged through the live
ExpandCollapseState read. `expanded` holds on state 1 only (2, PartiallyExpanded, does not);
`collapsed` holds on 0 only; a Button without the pattern reads `no_pattern`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.verify.predicates import STATE_ROLES, CheckState, Predicate, State
from steerai.verify.t1 import evaluate, redact
from tests.verify.fakes import BASE_ROWS, FakeHandle, FakeLive, Row, build

SHIPPING: Row = (30, "Button", "Shipping questions", (42, 70), 0)
ALERTS_BOX: Row = (32, "CheckBox", "Email alerts", (42, 72), 0)
EXPANDED = {"state": {"role": "Button", "name": "Shipping questions", "wanted": "expanded"}}
COLLAPSED = {"state": {"role": "Button", "name": "Shipping questions", "wanted": "collapsed"}}


def _state(wanted: Any = "expanded", role: str = "Button", name: str = "Shipping questions") -> Any:
    return CheckState(role=role, name_pattern=name, wanted=wanted)


# ---- the predicate -------------------------------------------------------------------------------


@pytest.mark.parametrize("wanted", ["expanded", "collapsed"])
def test_a_button_reads_expanded_or_collapsed(wanted: Any) -> None:
    assert _state(wanted).wanted == wanted
    assert STATE_ROLES[wanted] == frozenset({"Button"})
    assert State(target_id=5, wanted=wanted).wanted == wanted


@pytest.mark.parametrize(
    ("role", "wanted"),
    [
        ("Button", "selected"),
        ("CheckBox", "expanded"),
        ("CheckBox", "collapsed"),
        ("TabItem", "expanded"),
        ("RadioButton", "collapsed"),
        ("ListItem", "expanded"),
    ],
)
def test_the_role_and_the_wanted_value_must_agree(role: str, wanted: Any) -> None:
    with pytest.raises(ValueError, match="wanted for a"):
        CheckState(role=role, name_pattern="Shipping questions", wanted=wanted)


@pytest.mark.parametrize("role", ["TreeItem", "ComboBox", "MenuItem", "SplitButton", "Group"])
@pytest.mark.parametrize("wanted", ["expanded", "collapsed"])
def test_expanded_on_another_role_is_refused(role: str, wanted: Any) -> None:
    with pytest.raises(ValueError, match="role"):
        CheckState(role=role, name_pattern="Shipping questions", wanted=wanted)


@pytest.mark.parametrize("wanted", ["open", "closed", "partially_expanded", "EXPANDED"])
def test_an_unknown_wanted_is_refused(wanted: Any) -> None:
    with pytest.raises(ValueError, match="wanted"):
        State(target_id=5, wanted=wanted)
    with pytest.raises(ValueError, match="wanted"):
        CheckState(role="Button", name_pattern="Shipping questions", wanted=wanted)


# ---- the plan file -------------------------------------------------------------------------------


def _step(
    name: str = "Shipping questions", role: str = "Button", verb: str = "expand", **kw: Any
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": "s1",
        "subgoal": "g1",
        "verb": verb,
        "target": {"role": role, "name": name},
        "risk": "R1",
    }
    step.update(kw)
    return step


def _doc(steps: list[dict[str, Any]], condition: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "goal": "show the shipping questions",
            "subgoals": [{"id": "g1", "desc": "shown", "condition": condition or EXPANDED}],
            "steps": steps,
        }
    )


def test_an_expanded_state_parses_and_round_trips() -> None:
    plan = plan_from_json(_doc([_step()]))
    assert plan.subgoals[0].condition == _state()
    assert json.loads(plan_to_json(plan))["subgoals"][0]["condition"] == EXPANDED


def test_a_collapsed_state_is_proven_by_its_collapse() -> None:
    plan = plan_from_json(_doc([_step(verb="collapse")], COLLAPSED))
    assert plan.subgoals[0].condition == _state("collapsed")


@pytest.mark.parametrize(
    ("body", "path"),
    [
        (
            {"role": "Button", "name": "Shipping questions", "wanted": "selected"},
            "$.subgoals[0].condition.state.wanted",
        ),
        (
            {"role": "CheckBox", "name": "Email alerts", "wanted": "expanded"},
            "$.subgoals[0].condition.state.wanted",
        ),
        (
            {"role": "TabItem", "name": "Details", "wanted": "collapsed"},
            "$.subgoals[0].condition.state.wanted",
        ),
        (
            {"role": "TreeItem", "name": "Folders", "wanted": "expanded"},
            "$.subgoals[0].condition.state.role",
        ),
        (
            {"role": "Button", "name": "Shipping questions", "wanted": "open"},
            "$.subgoals[0].condition.state.wanted",
        ),
    ],
)
def test_a_disagreeing_state_is_refused_at_its_field(body: dict[str, Any], path: str) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([_step()], {"state": body}))
    assert caught.value.path == path, caught.value


@pytest.mark.parametrize(
    ("step", "condition"),
    [
        (_step(name="Help section"), EXPANDED),  # another button
        (_step(verb="collapse"), EXPANDED),  # a collapse never proves expanded
        (_step(verb="expand"), COLLAPSED),  # an expand never proves collapsed
        (
            _step(target={"role": "Button", "name": "Shipping questions", "automation_id": "acc"}),
            EXPANDED,
        ),
        (
            _step(
                verb="click",
                expect={"appeared": {"role": "Text", "name": "Orders ship within two days."}},
            ),
            EXPANDED,
        ),  # a click on the button is no proof of the state
    ],
)
def test_an_expanded_subgoal_must_name_its_expand_step(
    step: dict[str, Any], condition: dict[str, Any]
) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([step], condition))
    assert caught.value.path == "$.subgoals[0].condition"


def test_expanded_is_never_a_step_expect() -> None:
    click = _step(verb="click", expect=EXPANDED)
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([click], {"title": {"pattern": "x"}}))
    assert caught.value.path == "$.steps[0].expect.state"
    assert "own their postcondition" in caught.value.reason


# ---- T1: one snapshot, the live ExpandCollapseState read ----------------------------------------


def _judge(predicate: Predicate, live: Any, *rows: Row) -> Any:
    snapshot = build([*BASE_ROWS, *rows], snapshot_id="judged")
    return evaluate(predicate, snapshot, snapshot, live=live)


def _live(row: Row, *, expanded: int | None, toggle: int | None = None) -> FakeLive:
    return FakeLive(states={row[3]: FakeHandle(expanded=expanded, toggle=toggle)})


def test_expanded_holds_on_state_one_only() -> None:
    assert _judge(_state(), _live(SHIPPING, expanded=1), SHIPPING).holds is True
    for state in (0, 2, 3):
        verdict = _judge(_state(), _live(SHIPPING, expanded=state), SHIPPING)
        assert verdict.holds is False, state
        assert verdict.evidence == {"reason": "state_differs"}, state
        assert verdict.state == "state_differs"


def test_collapsed_holds_on_state_zero_only() -> None:
    assert _judge(_state("collapsed"), _live(SHIPPING, expanded=0), SHIPPING).holds is True
    for state in (1, 2, 3):
        verdict = _judge(_state("collapsed"), _live(SHIPPING, expanded=state), SHIPPING)
        assert verdict.holds is False, state
        assert verdict.evidence == {"reason": "state_differs"}, state


def test_a_button_without_the_pattern_settles_whatever_the_toggle_reads() -> None:
    verdict = _judge(_state(), _live(SHIPPING, expanded=None, toggle=1), SHIPPING)
    assert verdict.evidence == {"reason": "no_pattern"}
    assert verdict.state == "no_pattern"


def test_the_step_state_polls_the_expand_state_and_never_settles() -> None:
    """The step's own `State` polls to its deadline, as a toggle's does ([v2.57])."""
    held = _judge(
        State(target_id=SHIPPING[0], wanted="expanded"), _live(SHIPPING, expanded=1), SHIPPING
    )
    assert held.holds is True
    missed = _judge(
        State(target_id=SHIPPING[0], wanted="expanded"), _live(SHIPPING, expanded=2), SHIPPING
    )
    assert missed.holds is False
    assert missed.evidence == {"reason": "state_differs"}
    assert missed.state is None, "never settles: T1 polls to its deadline"


def test_a_checkbox_state_still_reads_the_toggle_not_the_expand_state() -> None:
    on = CheckState(role="CheckBox", name_pattern="Email alerts", wanted="on")
    verdict = _judge(on, _live(ALERTS_BOX, expanded=1, toggle=0), ALERTS_BOX)
    assert verdict.evidence == {"reason": "state_differs"}


def test_twin_buttons_are_ambiguous() -> None:
    twin: Row = (33, "Button", "shipping questions", (42, 73), 0)
    live = FakeLive(states={SHIPPING[3]: FakeHandle(expanded=1), twin[3]: FakeHandle(expanded=1)})
    verdict = _judge(_state(), live, SHIPPING, twin)
    assert verdict.evidence == {"reason": "ambiguous_target"}


def test_redaction_keeps_the_role_and_wanted_only() -> None:
    spec = redact(_state("collapsed", name="sentinel-button-1c4f"))
    assert spec == {"kind": "CheckState", "role": "Button", "wanted": "collapsed"}
    assert "sentinel" not in repr(_state("collapsed", name="sentinel-button-1c4f"))
