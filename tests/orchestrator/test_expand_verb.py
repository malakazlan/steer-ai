"""The `expand` / `collapse` plan verbs (widget-verbs W5, the post-H3 ruling, 2026-11-10): a
Button only, the semantic ExpandCollapsePattern path only, a code-owned `State(target,
expanded | collapsed)` template, an R1 floor, never preapproved, and every refusal at its field
path. Measured (probe `widget_patterns --section disclosure`, Chrome 156, n=3): an
`aria-expanded` Button exposes ExpandCollapse and no Invoke; Expand reads 1 and Collapse 0 within
11-22 ms with one trusted click each."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from steerai.orchestrator.contracts import (
    STATE_VERBS,
    WIDGET_VERBS,
    Step,
    Target,
    executor_verb,
    state_roles,
)
from steerai.orchestrator.gate import Ask, Gate, effective_risk
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


def _step(
    verb: str = "expand", role: str = "Button", name: str = "Shipping questions", **kw: Any
) -> Step:
    kw.setdefault("risk", "R1")
    return Step(id="s1", subgoal="g1", verb=verb, target=Target(role=role, name=name), **kw)


def _plan(**step: Any) -> str:
    body = {
        "goal": "show the shipping questions",
        "subgoals": [{"id": "g1", "desc": "shown", "condition": {"title": {"pattern": "x"}}}],
        "steps": [
            {
                "id": "s1",
                "subgoal": "g1",
                "verb": "expand",
                "target": {"role": "Button", "name": "Shipping questions"},
                **step,
            }
        ],
    }
    return json.dumps(body)


# ---- contracts ---------------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["expand", "collapse"])
def test_expand_and_collapse_run_on_the_semantic_expand_collapse_path(verb: str) -> None:
    assert executor_verb(verb) == ("semantic", verb)
    assert verb in STATE_VERBS
    assert verb in WIDGET_VERBS


def test_each_verb_maps_to_a_button_and_its_wanted_value() -> None:
    assert state_roles("expand") == frozenset({"Button"})
    assert state_roles("collapse") == frozenset({"Button"})
    assert STATE_WANTED["expand"] == "expanded"
    assert STATE_WANTED["collapse"] == "collapsed"


@pytest.mark.parametrize("verb", ["expand", "collapse"])
def test_the_verbs_are_raised_to_the_r1_floor(verb: str) -> None:
    assert _step(verb, risk="R0").risk == "R1"


@pytest.mark.parametrize("verb", ["expand", "collapse"])
@pytest.mark.parametrize(
    "role", ["TreeItem", "ComboBox", "MenuItem", "SplitButton", "CheckBox", "TabItem", "Group"]
)
def test_the_verbs_on_another_role_are_refused(verb: str, role: str) -> None:
    with pytest.raises(ValueError, match="targets a Button"):
        _step(verb, role=role)


@pytest.mark.parametrize("verb", ["check", "uncheck", "select"])
def test_the_other_state_verbs_still_refuse_a_button(verb: str) -> None:
    with pytest.raises(ValueError, match="targets a"):
        _step(verb, role="Button")


@pytest.mark.parametrize("verb", ["expand", "collapse"])
def test_the_verbs_never_run_on_the_physical_path(verb: str) -> None:
    with pytest.raises(ValueError, match="semantic"):
        _step(verb, path="physical")


@pytest.mark.parametrize("verb", ["expand", "collapse"])
def test_the_verbs_are_never_preapproved(verb: str) -> None:
    with pytest.raises(ValueError, match="preapproved"):
        _step(verb, preapproved_by_request=True)


@pytest.mark.parametrize("verb", ["expand", "collapse"])
def test_the_verbs_take_no_expect(verb: str) -> None:
    with pytest.raises(ValueError, match="template"):
        _step(verb, expect=State(target_id=3, wanted="expanded"))


def test_the_verbs_take_no_option() -> None:
    with pytest.raises(ValueError, match="option"):
        _step(params={"option": "Large"})


# ---- plan file (refusals at their field paths) --------------------------------------------------


@pytest.mark.parametrize("verb", ["expand", "collapse"])
def test_a_plan_file_step_decodes_at_the_r1_floor(verb: str) -> None:
    plan = plan_from_json(_plan(verb=verb, risk="R0"))
    assert plan.steps[0].verb == verb
    assert plan.steps[0].risk == "R1"
    assert plan.steps[0].target == Target(role="Button", name="Shipping questions")


@pytest.mark.parametrize(
    ("step", "path"),
    [
        ({"expect": {"appeared": {"role": "Text", "name": "Panel"}}}, "$.steps[0].expect"),
        ({"verb": "collapse", "expect": {"title": {"pattern": "x"}}}, "$.steps[0].expect"),
        ({"path": "physical"}, "$.steps[0].path"),
        ({"verb": "collapse", "path": "physical"}, "$.steps[0].path"),
        ({"preapproved_by_request": True}, "$.steps[0].preapproved_by_request"),
        ({"verb": "collapse", "preapproved_by_request": True}, "$.steps[0].preapproved_by_request"),
        ({"target": {"role": "TreeItem", "name": "Shipping questions"}}, "$.steps[0].target.role"),
        ({"target": {"role": "ComboBox", "name": "Shipping questions"}}, "$.steps[0].target.role"),
        ({"target": {"role": "MenuItem", "name": "Shipping questions"}}, "$.steps[0].target.role"),
        (
            {"verb": "collapse", "target": {"role": "CheckBox", "name": "Shipping questions"}},
            "$.steps[0].target.role",
        ),
        ({"verb": "check"}, "$.steps[0].target.role"),
        ({"verb": "select"}, "$.steps[0].target.role"),
        ({"params": {"option": "Large"}}, "$.steps[0].params.option"),
    ],
)
def test_a_plan_file_step_is_refused_at_its_field(step: dict[str, Any], path: str) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan(**step))
    assert caught.value.path == path, caught.value


# ---- template and strength ---------------------------------------------------------------------


def test_the_template_is_the_buttons_own_expand_state() -> None:
    assert template_for("expand", Params(target_id=5)) == State(target_id=5, wanted="expanded")
    assert template_for("collapse", Params(target_id=5)) == State(target_id=5, wanted="collapsed")


@pytest.mark.parametrize("tier", ["R2", "R3"])
@pytest.mark.parametrize(("verb", "wanted"), [("expand", "expanded"), ("collapse", "collapsed")])
def test_the_own_state_is_strong_at_r2_and_r3(tier: Any, verb: str, wanted: Any) -> None:
    params = Params(target_id=5)
    assert strength(State(target_id=5, wanted=wanted), tier, params, verb=verb).ok


def test_an_expanded_state_is_not_strong_for_a_click_at_r2() -> None:
    params = Params(target_id=5)
    expanded = State(target_id=5, wanted="expanded")
    assert strength(expanded, "R2", params, verb="click").ok is False
    assert strengthen(expanded, "click", "R2", params) is None


def test_another_value_another_element_or_a_nested_state_is_not_strong() -> None:
    params = Params(target_id=5)
    assert strength(State(target_id=5, wanted="collapsed"), "R2", params, verb="expand").ok is False
    assert (
        strength(State(target_id=5, wanted="expanded"), "R2", params, verb="collapse").ok is False
    )
    assert strength(State(target_id=6, wanted="expanded"), "R2", params, verb="expand").ok is False
    expanded = State(target_id=5, wanted="expanded")
    assert strength(All((expanded, expanded)), "R2", params, verb="expand").ok is False
    assert strength(expanded, "R2", params, verb="select").ok is False
    assert strength(expanded, "R2", params, verb="check").ok is False


# ---- tier and the gate -------------------------------------------------------------------------


def test_a_plain_disclosure_button_stays_r1() -> None:
    assert effective_risk(_step()) == "R1"
    assert effective_risk(_step("collapse", name="Help section")) == "R1"


def test_deny_and_control_words_on_the_button_raise_it_as_a_button() -> None:
    """A Button is in CONTROL_ROLES: the deny and control patterns read its name as today."""
    assert effective_risk(_step(name="Delete history")) == "R3"
    assert effective_risk(_step("collapse", name="Subscribe options")) == "R2"


def test_an_r1_expand_is_approved_with_its_template_unasked() -> None:
    confirmer = _Confirmer()
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    result = gate.decide(_step(), action_key="k", expect=None, target_pid=2, target_id=5)
    assert result.outcome == "approve", result.evidence
    assert result.expect == State(target_id=5, wanted="expanded")
    assert confirmer.asked == []


def test_an_expand_raised_by_a_deny_word_asks_with_its_own_state() -> None:
    confirmer = _Confirmer()
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    step = _step(name="Delete history")
    result = gate.decide(step, action_key="k", expect=None, target_pid=2, target_id=5)
    assert result.outcome == "confirm", result.evidence
    assert result.expect == State(target_id=5, wanted="expanded")
    assert [a.verb for a in confirmer.asked] == ["expand"]
    assert confirmer.asked[0].risk == "R3"
