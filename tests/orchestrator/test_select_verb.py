"""The `select` plan verb (widget-verbs packet W3 v1, its verdict and W3 v2, 2026-11-09): a
TabItem or a RadioButton only, the semantic SelectionItem.Select path only, a code-owned
`State(target, "selected")` template, an R1 floor, never preapproved, and every refusal at its
field path (W3-21)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from steerai.orchestrator.contracts import STATE_VERBS, Step, Target, executor_verb, state_roles
from steerai.orchestrator.gate import Ask, Gate
from steerai.orchestrator.plan_io import PlanError, plan_from_json
from steerai.verify.predicates import All, State
from steerai.verify.strength import STATE_WANTED, Params, strength, strengthen, template_for


@dataclass
class _Confirmer:
    default: str = "yes"
    asked: list[Ask] = field(default_factory=list)

    def ask(self, ask: Ask) -> str:
        self.asked.append(ask)
        return self.default


def _step(verb: str = "select", role: str = "TabItem", name: str = "Details", **kw: Any) -> Step:
    kw.setdefault("risk", "R1")
    return Step(id="s1", subgoal="g1", verb=verb, target=Target(role=role, name=name), **kw)


def _plan(**step: Any) -> str:
    body = {
        "goal": "switch to the details tab",
        "subgoals": [{"id": "g1", "desc": "details", "condition": {"title": {"pattern": "x"}}}],
        "steps": [
            {
                "id": "s1",
                "subgoal": "g1",
                "verb": "select",
                "target": {"role": "TabItem", "name": "Details"},
                **step,
            }
        ],
    }
    return json.dumps(body)


# ---- contracts ---------------------------------------------------------------------------------


def test_select_runs_on_the_semantic_selection_item_path() -> None:
    assert executor_verb("select") == ("semantic", "select")
    assert "select" in STATE_VERBS


def test_each_state_verb_maps_to_its_roles_and_its_wanted_value() -> None:
    assert state_roles("check") == frozenset({"CheckBox"})
    assert state_roles("uncheck") == frozenset({"CheckBox"})
    assert state_roles("select") == frozenset({"TabItem", "RadioButton", "ListItem"})
    assert dict(STATE_WANTED) == {"check": "on", "uncheck": "off", "select": "selected"}


@pytest.mark.parametrize("role", ["TabItem", "RadioButton", "ListItem"])
def test_select_targets_a_tab_a_radio_or_a_listbox_option(role: str) -> None:
    """W3b: a listbox option (ListItem) joins the tab and the radio; the loop admits it only
    under a single-select List (`_listbox_admission`)."""
    assert _step(role=role).target == Target(role=role, name="Details")


def test_select_is_raised_to_the_r1_floor() -> None:
    assert _step(risk="R0").risk == "R1"


@pytest.mark.parametrize("role", ["CheckBox", "TreeItem", "DataItem", "Button", "Hyperlink"])
def test_select_on_another_role_is_refused(role: str) -> None:
    with pytest.raises(ValueError, match="ListItem or RadioButton or TabItem"):
        _step(role=role)


@pytest.mark.parametrize("verb", ["check", "uncheck"])
@pytest.mark.parametrize("role", ["RadioButton", "TabItem"])
def test_check_or_uncheck_on_a_radio_or_a_tab_is_refused(verb: str, role: str) -> None:
    with pytest.raises(ValueError, match="CheckBox"):
        _step(verb=verb, role=role)


def test_select_never_runs_on_the_physical_path() -> None:
    with pytest.raises(ValueError, match="semantic"):
        _step(path="physical")


def test_select_is_never_preapproved() -> None:
    with pytest.raises(ValueError, match="preapproved"):
        _step(preapproved_by_request=True)


def test_select_takes_no_expect() -> None:
    with pytest.raises(ValueError, match="template"):
        _step(expect=State(target_id=3, wanted="selected"))


# ---- plan file (W3-21: refusals at their field paths) ------------------------------------------


def test_a_plan_file_select_step_decodes_at_the_r1_floor() -> None:
    plan = plan_from_json(_plan(risk="R0"))
    assert plan.steps[0].verb == "select"
    assert plan.steps[0].risk == "R1"


def test_a_plan_file_select_on_a_listbox_option_decodes() -> None:
    plan = plan_from_json(_plan(target={"role": "ListItem", "name": "Kyoto"}))
    assert plan.steps[0].target == Target(role="ListItem", name="Kyoto")


@pytest.mark.parametrize(
    ("step", "path"),
    [
        ({"expect": {"appeared": {"role": "Text", "name": "Panel"}}}, "$.steps[0].expect"),
        ({"path": "physical"}, "$.steps[0].path"),
        ({"preapproved_by_request": True}, "$.steps[0].preapproved_by_request"),
        ({"target": {"role": "CheckBox", "name": "Details"}}, "$.steps[0].target.role"),
        ({"target": {"role": "DataItem", "name": "Details"}}, "$.steps[0].target.role"),
        ({"target": {"role": "TreeItem", "name": "Details"}}, "$.steps[0].target.role"),
        (
            {"verb": "uncheck", "target": {"role": "RadioButton", "name": "Annual"}},
            "$.steps[0].target.role",
        ),
        (
            {"verb": "check", "target": {"role": "RadioButton", "name": "Annual"}},
            "$.steps[0].target.role",
        ),
        ({"verb": "check", "preapproved_by_request": True}, "$.steps[0].target.role"),
    ],
)
def test_a_plan_file_state_step_is_refused_at_its_field(step: dict[str, Any], path: str) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan(**step))
    assert caught.value.path == path, caught.value


def test_a_plan_file_check_preapproved_is_refused_at_its_field() -> None:
    body = {"verb": "check", "target": {"role": "CheckBox", "name": "Email alerts"}}
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan(**body, preapproved_by_request=True))
    assert caught.value.path == "$.steps[0].preapproved_by_request"


# ---- template and strength ---------------------------------------------------------------------


def test_the_template_is_the_targets_own_selected_state() -> None:
    assert template_for("select", Params(target_id=5)) == State(target_id=5, wanted="selected")


@pytest.mark.parametrize("tier", ["R2", "R3"])
def test_the_own_selected_state_is_strong_at_r2_and_r3(tier: Any) -> None:
    params = Params(target_id=5)
    assert strength(State(target_id=5, wanted="selected"), tier, params, verb="select").ok


def test_a_selected_state_is_not_strong_for_a_click_at_r2() -> None:
    params = Params(target_id=5)
    selected = State(target_id=5, wanted="selected")
    assert strength(selected, "R2", params, verb="click").ok is False
    assert strengthen(selected, "click", "R2", params) is None


def test_another_value_another_element_or_a_nested_state_is_not_strong_for_select() -> None:
    params = Params(target_id=5)
    assert strength(State(target_id=5, wanted="on"), "R2", params, verb="select").ok is False
    assert strength(State(target_id=6, wanted="selected"), "R2", params, verb="select").ok is False
    selected = State(target_id=5, wanted="selected")
    assert strength(All((selected, selected)), "R2", params, verb="select").ok is False
    assert strength(State(target_id=5, wanted="selected"), "R2", params, verb="check").ok is False


def test_a_r1_select_is_approved_with_its_template_unasked() -> None:
    confirmer = _Confirmer()
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    result = gate.decide(_step(), action_key="k", expect=None, target_pid=2, target_id=5)
    assert result.outcome == "approve", result.evidence
    assert result.expect == State(target_id=5, wanted="selected")
    assert confirmer.asked == []
