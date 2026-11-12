"""Verbs admitted by capability (packet interaction-model-2026-11-12 §2; principal
APPROVE-WITH-CHANGES items 2 and 4). Measured on the gallery (Chrome 156, `gallery_acts`, n=2):
TogglePattern.Toggle on a `role=switch` Button and an `aria-pressed` Button flips it and the page
sees a trusted click; SelectionItem.Select on a grid row selects it; Chrome exposes
menuitemcheckbox as a CheckBox and menuitemradio as a RadioButton. Expand on an ARIA tree item
does nothing, so TreeItem stays refused (and MenuItem: never a state target)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.orchestrator.contracts import Step, Target, state_roles
from steerai.orchestrator.gate import effective_risk
from steerai.orchestrator.plan_io import PlanError, plan_from_bytes
from tests.orchestrator.fakes import BASE_ROWS, Row
from tests.orchestrator.test_loop import Harness
from tests.orchestrator.test_state_verbs_loop import Boxes


def _plan_bytes(verb: str, role: str, name: str) -> bytes:
    return json.dumps(
        {
            "goal": "g",
            "subgoals": [{"id": "g1", "desc": "d", "condition": {"title": {"pattern": "Never"}}}],
            "steps": [
                {
                    "id": "s1",
                    "subgoal": "g1",
                    "verb": verb,
                    "target": {"role": role, "name": name},
                    "risk": "R1",
                }
            ],
        }
    ).encode("utf-8")


def test_the_role_ceilings_are_the_measured_ones() -> None:
    assert state_roles("check") == frozenset({"CheckBox", "Button"})
    assert state_roles("uncheck") == frozenset({"CheckBox", "Button"})
    assert state_roles("select") == frozenset({"TabItem", "RadioButton", "ListItem", "DataItem"})
    assert state_roles("expand") == frozenset({"Button"})
    for verb in ("check", "select", "expand"):
        assert "TreeItem" not in state_roles(verb), "Expand on an ARIA tree item does nothing"
        assert "MenuItem" not in state_roles(verb), "menu checkboxes and radios are CheckBox/Radio"


@pytest.mark.parametrize(
    ("verb", "role"),
    [("check", "Button"), ("uncheck", "Button"), ("select", "DataItem")],
)
def test_a_measured_capability_target_loads(verb: str, role: str) -> None:
    plan = plan_from_bytes(_plan_bytes(verb, role, "Dark mode"))
    assert plan.steps[0].target is not None
    assert plan.steps[0].target.role == role


@pytest.mark.parametrize(("verb", "role"), [("expand", "TreeItem"), ("check", "MenuItem")])
def test_an_unmeasured_capability_target_is_refused_at_its_path(verb: str, role: str) -> None:
    with pytest.raises(PlanError) as refused:
        plan_from_bytes(_plan_bytes(verb, role, "Projects"))
    assert refused.value.path == "$.steps[0].target.role"


# ---- the safety classes read every state-verb target (principal item 4) ---------------------


def _step(verb: str, role: str, name: str) -> Step:
    return Step(id="s1", subgoal="g1", verb=verb, target=Target(role=role, name=name), risk="R1")


def test_a_consent_switch_is_r2_like_a_consent_checkbox() -> None:
    assert effective_risk(_step("check", "CheckBox", "I agree to share my data")) == "R2"
    assert effective_risk(_step("check", "Button", "I agree to share my data")) == "R2"


def test_a_billed_grid_row_is_r2_like_a_billed_radio() -> None:
    assert effective_risk(_step("select", "RadioButton", "Annual plan $99/year")) == "R2"
    assert effective_risk(_step("select", "DataItem", "Annual plan $99/year")) == "R2"


def test_a_plain_switch_and_a_plain_row_stay_at_their_plan_tier() -> None:
    assert effective_risk(_step("check", "Button", "Dark mode")) == "R1"
    assert effective_risk(_step("select", "DataItem", "Order 1001")) == "R1"


def test_a_click_on_a_button_named_agree_is_not_raised_by_the_consent_class() -> None:
    """Consent is a statement made by setting a value; a click keeps its own rules."""
    assert effective_risk(_step("click", "Button", "I agree")) == "R1"
    assert effective_risk(_step("uncheck", "Button", "I agree to share my data")) == "R2"


def test_the_consent_class_reads_a_switch_s_automation_id() -> None:
    switch = Step(
        id="s1",
        subgoal="g1",
        verb="check",
        target=Target(role="Button", name="Terms", automation_id="agreeTerms"),
        risk="R1",
    )
    assert effective_risk(switch) == "R2"


def test_billing_reads_a_switch_s_group_name_as_a_checkbox_s() -> None:
    """Principal blocking item 1: a switch "Auto-renew" in a "Subscription" fieldset is a
    billing choice, as the same CheckBox is (W3-02); a "Pay" group is no billing word."""
    switch = _step("check", "Button", "Auto-renew")
    assert effective_risk(switch, group_name="Subscription") == "R2"
    assert (
        effective_risk(_step("check", "CheckBox", "Auto-renew"), group_name="Subscription") == "R2"
    )
    assert effective_risk(switch, group_name="Pay") == "R1"
    assert (
        effective_risk(_step("select", "TabItem", "Auto-renew"), group_name="Subscription") == "R1"
    )


# ---- a grid row: the row is the selection unit, never its same-named cell -------------------

GRID: Row = (60, "DataGrid", "Orders", (42, 160), None)
ROW: Row = (61, "DataItem", "Order 1001", (42, 161), 60)
CELL: Row = (62, "DataItem", "Order 1001", (42, 162), 61)
ROW2: Row = (63, "DataItem", "Order 1002", (42, 163), 60)
MULTI: Row = (64, "DataGrid", "Files", (42, 164), None)
FILE: Row = (65, "DataItem", "report.pdf", (42, 165), 64)


def _grid_harness() -> tuple[Harness, Boxes]:
    h = Harness()
    h.world.rows = [*BASE_ROWS, GRID, ROW, CELL, ROW2, MULTI, FILE]
    boxes = Boxes(
        h.world,
        state={},
        selected={ROW[3]: False, CELL[3]: False, ROW2[3]: False, FILE[3]: False},
    )
    boxes.containers.update({GRID[3]: False, MULTI[3]: True})
    h.session._live = boxes

    def select(_ex: Any, action: Any) -> None:
        row = next(r for r in h.world.rows if r[0] == action.element_id)
        boxes.selected[row[3]] = True

    h.semantic.on_execute = select
    return h, boxes


def test_a_grid_row_select_resolves_the_row_not_its_cell() -> None:
    h, boxes = _grid_harness()
    result = h.session.run_step(_step("select", "DataItem", "Order 1001"))
    assert result.ok is True, result.to_dict()
    assert boxes.selected[ROW[3]] is True
    assert boxes.selected[CELL[3]] is False


def test_a_row_of_a_multi_select_grid_is_refused_before_the_gate() -> None:
    h, _boxes = _grid_harness()
    result = h.session.run_step(_step("select", "DataItem", "report.pdf"))
    assert result.ok is False
    assert result.evidence.get("detail") == "multi_select", result.to_dict()
    assert h.semantic.calls == []
