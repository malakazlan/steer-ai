"""The `state` condition with wanted `selected` (W3-5, W3 verdict ruling (c), W3-05): one kind
for on / off / selected, where the role and the wanted value agree (a CheckBox is on or off; a
TabItem or a RadioButton is selected), proven by a `select` step of the same subgoal on that
exact element, and judged through the live SelectionItem read."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.verify.predicates import CheckState, Predicate
from steerai.verify.t1 import evaluate, redact
from tests.verify.fakes import BASE_ROWS, FakeHandle, FakeLive, Row, build

DETAILS_TAB: Row = (30, "TabItem", "Details", (42, 70), 0)
ANNUAL_RADIO: Row = (31, "RadioButton", "Annual", (42, 71), 0)
ALERTS_BOX: Row = (32, "CheckBox", "Email alerts", (42, 72), 0)
SELECTED = {"state": {"role": "TabItem", "name": "Details", "wanted": "selected"}}


def _selected(role: str = "TabItem", name: str = "Details") -> CheckState:
    return CheckState(role=role, name_pattern=name, wanted="selected")


# ---- the predicate -------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["TabItem", "RadioButton", "ListItem"])
def test_a_tab_a_radio_or_a_listbox_option_reads_selected(role: str) -> None:
    assert _selected(role).wanted == "selected"


@pytest.mark.parametrize(
    ("role", "wanted"),
    [
        ("CheckBox", "selected"),
        ("TabItem", "on"),
        ("TabItem", "off"),
        ("RadioButton", "on"),
        ("RadioButton", "off"),
        ("ListItem", "on"),
        ("ListItem", "off"),
    ],
)
def test_the_role_and_the_wanted_value_must_agree(role: str, wanted: Any) -> None:
    with pytest.raises(ValueError, match="wanted for a"):
        CheckState(role=role, name_pattern="Details", wanted=wanted)


@pytest.mark.parametrize("role", ["TreeItem", "MenuItem", "Hyperlink"])
def test_selected_on_another_role_is_refused(role: str) -> None:
    with pytest.raises(ValueError, match="role"):
        CheckState(role=role, name_pattern="Details", wanted="selected")


# ---- the plan file -------------------------------------------------------------------------------


def _select_step(
    name: str = "Details", role: str = "TabItem", verb: str = "select", **kw: Any
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
            "goal": "switch to the details tab",
            "subgoals": [{"id": "g1", "desc": "details", "condition": condition or SELECTED}],
            "steps": steps,
        }
    )


def test_a_selected_state_parses_and_round_trips() -> None:
    plan = plan_from_json(_doc([_select_step()]))
    assert plan.subgoals[0].condition == _selected()
    assert json.loads(plan_to_json(plan))["subgoals"][0]["condition"] == SELECTED


def test_a_radio_selected_state_is_proven_by_its_select() -> None:
    radio = {"state": {"role": "RadioButton", "name": "Annual", "wanted": "selected"}}
    plan = plan_from_json(_doc([_select_step("Annual", "RadioButton")], radio))
    assert plan.subgoals[0].condition == _selected("RadioButton", "Annual")


def test_a_listbox_option_selected_state_is_proven_by_its_select() -> None:
    """W3b: a listbox option is a ListItem with wanted selected."""
    option = {"state": {"role": "ListItem", "name": "Kyoto", "wanted": "selected"}}
    plan = plan_from_json(_doc([_select_step("Kyoto", "ListItem")], option))
    assert plan.subgoals[0].condition == _selected("ListItem", "Kyoto")


@pytest.mark.parametrize(
    ("body", "path"),
    [
        (
            {"role": "CheckBox", "name": "Email alerts", "wanted": "selected"},
            "$.subgoals[0].condition.state.wanted",
        ),
        (
            {"role": "TabItem", "name": "Details", "wanted": "on"},
            "$.subgoals[0].condition.state.wanted",
        ),
        (
            {"role": "RadioButton", "name": "Annual", "wanted": "off"},
            "$.subgoals[0].condition.state.wanted",
        ),
        (
            {"role": "TreeItem", "name": "Details", "wanted": "selected"},
            "$.subgoals[0].condition.state.role",
        ),
        (
            {"role": "ListItem", "name": "Kyoto", "wanted": "on"},
            "$.subgoals[0].condition.state.wanted",
        ),
        (
            {"role": "TabItem", "name": "Details", "wanted": "chosen"},
            "$.subgoals[0].condition.state.wanted",
        ),
    ],
)
def test_a_disagreeing_state_is_refused_at_its_field(body: dict[str, Any], path: str) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([_select_step()], {"state": body}))
    assert caught.value.path == path, caught.value


@pytest.mark.parametrize(
    "step",
    [
        _select_step(name="Overview"),  # another tab
        _select_step(target={"role": "TabItem", "name": "Details", "automation_id": "t2"}),
        _select_step(
            verb="click",
            expect={
                "present": {"role": "TabItem", "name": "Details", "within": {"landmark": "Tabs"}}
            },
        ),  # a click on the tab is no proof of the state
    ],
)
def test_a_selected_subgoal_must_name_its_select_step(step: dict[str, Any]) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([step]))
    assert caught.value.path == "$.subgoals[0].condition"


def test_a_select_of_a_radio_never_proves_a_tab_of_the_same_name() -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([_select_step(role="RadioButton")]))
    assert caught.value.path == "$.subgoals[0].condition"


def test_selected_is_never_a_step_expect() -> None:
    click = _select_step(verb="click", role="Button", expect=SELECTED)
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc([click], {"title": {"pattern": "x"}}))
    assert caught.value.path == "$.steps[0].expect.state"
    assert (
        "check, uncheck, select, expand and collapse own their postcondition" in caught.value.reason
    )


# ---- T1: one snapshot, the live SelectionItem read ----------------------------------------------


def _judge(predicate: Predicate, live: Any, *rows: Row) -> Any:
    snapshot = build([*BASE_ROWS, *rows], snapshot_id="judged")
    return evaluate(predicate, snapshot, snapshot, live=live)


def _live(row: Row, *, selected: bool | None, toggle: int | None = None) -> FakeLive:
    return FakeLive(states={row[3]: FakeHandle(selected=selected, toggle=toggle)})


@pytest.mark.parametrize("row", [DETAILS_TAB, ANNUAL_RADIO])
def test_selected_holds_on_the_live_is_selected_read(row: Row) -> None:
    verdict = _judge(_selected(row[1], row[2]), _live(row, selected=True), row)
    assert verdict.holds is True


def test_not_selected_differs_and_settles_whatever_the_toggle_reads() -> None:
    verdict = _judge(_selected(), _live(DETAILS_TAB, selected=False, toggle=1), DETAILS_TAB)
    assert verdict.holds is False
    assert verdict.evidence == {"reason": "state_differs"}
    assert verdict.state == "state_differs"


def test_a_tab_without_a_selection_item_settles() -> None:
    verdict = _judge(_selected(), _live(DETAILS_TAB, selected=None, toggle=1), DETAILS_TAB)
    assert verdict.evidence == {"reason": "no_pattern"}


def test_a_checkbox_state_still_reads_the_toggle_not_the_selection() -> None:
    on = CheckState(role="CheckBox", name_pattern="Email alerts", wanted="on")
    verdict = _judge(on, _live(ALERTS_BOX, selected=True, toggle=0), ALERTS_BOX)
    assert verdict.evidence == {"reason": "state_differs"}


def test_twin_tabs_are_ambiguous() -> None:
    twin: Row = (33, "TabItem", "details", (42, 73), 0)
    live = FakeLive(
        states={DETAILS_TAB[3]: FakeHandle(selected=True), twin[3]: FakeHandle(selected=True)}
    )
    verdict = _judge(_selected(), live, DETAILS_TAB, twin)
    assert verdict.evidence == {"reason": "ambiguous_target"}


def test_redaction_keeps_the_role_and_wanted_only() -> None:
    spec = redact(_selected("RadioButton", "sentinel-radio-1c4f"))
    assert spec == {"kind": "CheckState", "role": "RadioButton", "wanted": "selected"}
