"""The `check` / `uncheck` plan verbs (widget-verbs packet W2 v2 and its verdict, 2026-11-08):
a CheckBox only, the semantic Toggle path only, a code-owned `State` template, an R1 floor, the
consent class, and the verb-scoped strength rule that lets the person be asked at R2/R3."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target, executor_verb
from steerai.orchestrator.gate import Ask, Gate, deny_tier, effective_risk
from steerai.orchestrator.plan_io import PlanError, plan_from_json
from steerai.verify.predicates import All, Appeared, State, Within
from steerai.verify.strength import Params, strength, strengthen, template_for


@dataclass
class _Confirmer:
    default: str = "yes"
    asked: list[Ask] = field(default_factory=list)

    def ask(self, ask: Ask) -> str:
        self.asked.append(ask)
        return self.default


def _step(verb: str = "check", name: str = "Email alerts", **kw: Any) -> Step:
    kw.setdefault("risk", "R1")
    role = kw.pop("role", "CheckBox")
    return Step(id="s1", subgoal="g1", verb=verb, target=Target(role=role, name=name), **kw)


def _plan(step: dict[str, Any]) -> str:
    import json  # noqa: PLC0415 - local to the fixture

    body = {
        "goal": "turn on email alerts",
        "subgoals": [{"id": "g1", "desc": "alerts on", "condition": {"title": {"pattern": "x"}}}],
        "steps": [
            {
                "id": "s1",
                "subgoal": "g1",
                "verb": "check",
                "target": {"role": "CheckBox", "name": "Email alerts"},
                **step,
            }
        ],
    }
    return json.dumps(body)


# ---- contracts ---------------------------------------------------------------------------------


def test_check_and_uncheck_run_on_the_semantic_toggle() -> None:
    assert executor_verb("check") == ("semantic", "check")
    assert executor_verb("uncheck") == ("semantic", "uncheck")


def test_a_state_verb_is_raised_to_the_r1_floor() -> None:
    assert _step(risk="R0").risk == "R1"
    assert _step(verb="uncheck", risk="R0").risk == "R1"


@pytest.mark.parametrize("role", ["RadioButton", "ListItem", "Edit", "MenuItem", "TreeItem"])
def test_a_state_verb_targets_a_toggling_control_only(role: str) -> None:
    """Toggle's role ceiling: a CheckBox or a Button (interaction model §2)."""
    with pytest.raises(ValueError, match="CheckBox"):
        _step(role=role)


def test_a_state_verb_never_runs_on_the_physical_path() -> None:
    with pytest.raises(ValueError, match="semantic"):
        _step(path="physical")


def test_a_state_verb_is_never_preapproved() -> None:
    with pytest.raises(ValueError, match="preapproved"):
        _step(preapproved_by_request=True)


# ---- plan file ---------------------------------------------------------------------------------


def test_a_plan_file_check_step_decodes() -> None:
    plan = plan_from_json(_plan({}))
    assert plan.steps[0].verb == "check"
    assert plan.steps[0].risk == "R1"


def test_a_plan_written_expect_on_a_state_verb_is_refused() -> None:
    expect = {"appeared": {"role": "Text", "name": "Alerts on"}}
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan({"expect": expect}))
    assert caught.value.path == "$.steps[0].expect"
    assert "template" in caught.value.reason


def test_a_plan_file_check_on_the_physical_path_is_refused() -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan({"path": "physical"}))
    assert caught.value.path == "$.steps[0].path"


def test_a_plan_file_check_on_a_menu_item_is_refused() -> None:
    with pytest.raises(PlanError, match="CheckBox"):
        plan_from_json(_plan({"target": {"role": "MenuItem", "name": "Email alerts"}}))


# ---- template and strength ---------------------------------------------------------------------


def test_the_template_is_the_targets_own_state() -> None:
    params = Params(target_id=5)
    assert template_for("check", params) == State(target_id=5, wanted="on")
    assert template_for("uncheck", params) == State(target_id=5, wanted="off")


@pytest.mark.parametrize("tier", ["R2", "R3"])
def test_the_verbs_own_state_on_the_resolved_element_is_strong_at_r2_and_r3(tier: Any) -> None:
    params = Params(target_id=5)
    assert strength(State(target_id=5, wanted="on"), tier, params, verb="check").ok
    assert strength(State(target_id=5, wanted="off"), tier, params, verb="uncheck").ok


def test_a_state_is_not_strong_for_a_click_at_r2() -> None:
    params = Params(target_id=5)
    verdict = strength(State(target_id=5, wanted="on"), "R2", params, verb="click")
    assert verdict.ok is False
    assert strengthen(State(target_id=5, wanted="on"), "click", "R2", params) is None


def test_a_state_inside_all_is_not_strong_at_r2() -> None:
    params = Params(target_id=5)
    nested = All((State(target_id=5, wanted="on"), State(target_id=5, wanted="on")))
    assert strength(nested, "R2", params, verb="check").ok is False


def test_a_state_on_another_element_or_the_other_value_is_not_strong_at_r2() -> None:
    params = Params(target_id=5)
    assert strength(State(target_id=6, wanted="on"), "R2", params, verb="check").ok is False
    assert strength(State(target_id=5, wanted="off"), "R2", params, verb="check").ok is False
    assert strength(State(target_id=5, wanted="on"), "R2", Params(), verb="check").ok is False


# ---- tier and gate -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "I agree to the terms",
        "I consent to processing",
        "Authorize the app",
        "Authorise the app",
        "I acknowledge the policy",
        "I certify this is true",
    ],
)
def test_the_consent_class_raises_a_checkbox_to_r2(name: str) -> None:
    assert deny_tier(_step(name=name)) == "R2"
    assert effective_risk(_step(name=name)) == "R2"


def test_the_consent_class_reads_whole_words_on_a_checkbox_only() -> None:
    assert deny_tier(_step(name="Agreement summary emails")) == "R0"
    button = Step(id="s1", subgoal="g1", verb="click", target=Target(role="Button", name="I agree"))
    assert deny_tier(button) == "R0"


def test_a_consent_box_asks_and_names_the_verb_and_the_wanted_state() -> None:
    confirmer = _Confirmer()
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    step = _step(name="I accept the terms")
    result = gate.decide(step, action_key="k", expect=None, target_pid=2, target_id=5)
    assert result.outcome == "confirm", result.evidence
    assert result.expect == State(target_id=5, wanted="on")
    assert len(confirmer.asked) == 1
    ask = confirmer.asked[0]
    assert ask.verb == "check"
    assert ask.expect == {"kind": "State", "target_id": 5, "wanted": "on"}
    assert ask.target_name == "I accept the terms"
    assert ask.risk == "R2"


def test_a_click_with_a_state_expect_at_r2_is_blocked_without_asking() -> None:
    confirmer = _Confirmer()
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    step = Step(
        id="s1",
        subgoal="g1",
        verb="click",
        target=Target(role="CheckBox", name="I accept the terms"),
        expect=State(target_id=5, wanted="on"),
    )
    result = gate.decide(step, action_key="k", expect=step.expect, target_pid=2, target_id=5)
    assert result.outcome == "block"
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "weak_predicate"
    assert confirmer.asked == []


def test_a_r1_check_is_approved_with_its_template_unasked() -> None:
    confirmer = _Confirmer()
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    result = gate.decide(_step(), action_key="k", expect=None, target_pid=2, target_id=5)
    assert result.outcome == "approve"
    assert result.expect == State(target_id=5, wanted="on")
    assert confirmer.asked == []


def test_an_unrelated_strong_predicate_is_unchanged_by_the_verb_rule() -> None:
    within = Within(landmark="Conversation")
    params = Params(target_id=5, text="hello there")
    appeared = Appeared(role="Text", name_pattern="hello there", within=within)
    assert strength(appeared, "R2", params).ok
    assert strength(appeared, "R2", params, verb="check").ok
