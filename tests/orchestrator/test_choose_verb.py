"""The `choose` plan verb (widget-verbs W4 v1-v3, the verdicts and the principal's W4 v3
corrections): a ComboBox only with `params.option`, the semantic path only, a code-owned
`ValueEquals(combobox, option)` template under the folding comparator (W4-05), an R1 floor, never
preapproved, the option verbatim in the goal (WV-13), a `value` condition proven by a choose,
the tier read on the option, the ComboBox's name and its group (W4-03), and the goal-match row
carrying the option (W4 v1 verdict item 5)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from steerai.goal_match import GOAL_MATCH_PROMPT_VERSION, build_prompt
from steerai.orchestrator.contracts import (
    STATE_VERBS,
    VERBS,
    WIDGET_VERBS,
    Step,
    Target,
    executor_verb,
    widget_roles,
)
from steerai.orchestrator.gate import Ask, Gate, deny_tier, effective_risk
from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.verify.predicates import All, FieldValue, ValueEquals
from steerai.verify.strength import Params, strength, strengthen, template_for

NBSP = chr(0xA0)
GOAL = "set the size to Large"


@dataclass
class _Confirmer:
    default: str = "yes"
    asked: list[Ask] = field(default_factory=list)

    def ask(self, ask: Ask) -> str:
        self.asked.append(ask)
        return self.default


def _step(option: str | None = "Large", name: str = "Size", **kw: Any) -> Step:
    kw.setdefault("risk", "R1")
    params = {} if option is None else {"option": option}
    return Step(
        id="s1",
        subgoal="g1",
        verb="choose",
        target=Target(role=kw.pop("role", "ComboBox"), name=name),
        params=params,
        **kw,
    )


def _plan(goal: str = GOAL, condition: Any = None, **step: Any) -> str:
    body = {
        "goal": goal,
        "subgoals": [
            {
                "id": "g1",
                "desc": "size set",
                "condition": condition or {"title": {"pattern": "x"}},
            }
        ],
        "steps": [
            {
                "id": "s1",
                "subgoal": "g1",
                "verb": "choose",
                "target": {"role": "ComboBox", "name": "Size"},
                "params": {"option": "Large"},
                **step,
            }
        ],
    }
    return json.dumps(body)


def _value(text: str = "Large", role: str = "ComboBox") -> dict[str, Any]:
    return {"value": {"role": role, "name": "Size", "text": text}}


# ---- contracts (W4-1, W4-01) ---------------------------------------------------------------------


def test_choose_is_a_widget_verb_on_the_semantic_choose_path() -> None:
    assert "choose" in VERBS
    assert executor_verb("choose") == ("semantic", "choose")
    assert STATE_VERBS | {"choose"} == WIDGET_VERBS
    assert widget_roles("choose") == frozenset({"ComboBox"})
    assert widget_roles("select") == frozenset({"TabItem", "RadioButton"})


def test_choose_is_raised_to_the_r1_floor() -> None:
    assert _step(risk="R0").risk == "R1"


@pytest.mark.parametrize("role", ["Edit", "List", "ListItem", "Button", "Document"])
def test_choose_on_another_role_is_refused(role: str) -> None:
    with pytest.raises(ValueError, match="ComboBox"):
        _step(role=role)


def test_choose_needs_an_option() -> None:
    with pytest.raises(ValueError, match="option"):
        _step(option=None)
    with pytest.raises(ValueError, match="option"):
        _step(option="  ")


def test_an_option_with_an_invisible_character_is_refused() -> None:
    with pytest.raises(ValueError, match="option"):
        _step(option="La" + chr(0x200B) + "rge")


@pytest.mark.parametrize("verb", ["click", "select", "type"])
def test_no_other_verb_takes_an_option(verb: str) -> None:
    roles = {"click": "Button", "select": "TabItem", "type": "Edit"}
    with pytest.raises(ValueError, match="option"):
        Step(
            id="s1",
            subgoal="g1",
            verb=verb,
            target=Target(role=roles[verb], name="Size"),
            text="x" if verb == "type" else None,
            params={"option": "Large"},
        )


def test_choose_never_runs_on_the_physical_path() -> None:
    with pytest.raises(ValueError, match="semantic"):
        _step(path="physical")


def test_choose_is_never_preapproved() -> None:
    with pytest.raises(ValueError, match="preapproved"):
        _step(preapproved_by_request=True)


def test_choose_takes_no_expect() -> None:
    with pytest.raises(ValueError, match="template"):
        _step(expect=ValueEquals(target_id=3, text="Large"))


def test_the_option_stays_out_of_the_step_repr() -> None:
    assert "Large" not in repr(_step())


# ---- plan file: refusals at their field paths ----------------------------------------------------


def test_a_plan_file_choose_decodes_at_the_r1_floor_with_its_option() -> None:
    plan = plan_from_json(_plan(risk="R0"))
    assert plan.steps[0].verb == "choose"
    assert plan.steps[0].risk == "R1"
    assert plan.steps[0].params["option"] == "Large"
    assert json.loads(plan_to_json(plan))["steps"][0]["params"] == {"option": "Large"}


@pytest.mark.parametrize(
    ("step", "path"),
    [
        ({"expect": {"appeared": {"role": "Text", "name": "Large"}}}, "$.steps[0].expect"),
        ({"path": "physical"}, "$.steps[0].path"),
        ({"preapproved_by_request": True}, "$.steps[0].preapproved_by_request"),
        ({"target": {"role": "Edit", "name": "Size"}}, "$.steps[0].target.role"),
        ({"target": {"role": "ListItem", "name": "Large"}}, "$.steps[0].target.role"),
        ({"params": {}}, "$.steps[0].params.option"),
        ({"params": {"option": "Medium"}}, "$.steps[0].params.option"),
    ],
)
def test_a_plan_file_choose_is_refused_at_its_field(step: dict[str, Any], path: str) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan(**step))
    assert caught.value.path == path, caught.value


def test_an_option_not_in_the_goal_is_screen_content() -> None:
    # WV-13: the person names the option; one read off the screen is refused
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan(goal="pick a bigger size"))
    assert caught.value.path == "$.steps[0].params.option"
    assert "screen_content" in caught.value.reason


def test_an_option_is_verbatim_in_the_goal_through_nfkc_and_whitespace() -> None:
    plan = plan_from_json(
        _plan(goal=f"set the city to New{NBSP}York", params={"option": "New York"})
    )
    assert plan.steps[0].params["option"] == "New York"


@pytest.mark.parametrize("verb", ["click", "check"])
def test_an_option_on_another_verb_is_refused_at_its_field(verb: str) -> None:
    roles = {"click": "Button", "check": "CheckBox"}
    body: dict[str, Any] = {"verb": verb, "target": {"role": roles[verb], "name": "Size"}}
    if verb == "click":
        body["expect"] = {"appeared": {"role": "Text", "name": "Large"}}
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan(**body))
    assert caught.value.path == "$.steps[0].params.option", caught.value


# ---- the value condition proven by a choose (W4-7, W4 v1 verdict item 4, W4-05) -----------------


def test_a_value_on_the_combobox_is_proven_by_its_choose() -> None:
    plan = plan_from_json(_plan(condition=_value()))
    value = plan.subgoals[0].condition
    assert isinstance(value, FieldValue)
    assert value.folded is True, "a choose's value uses the folding comparator"


def test_the_choose_to_value_mapping_folds_a_no_break_space() -> None:
    plan = plan_from_json(
        _plan(
            goal=f"set the city to New{NBSP}York",
            condition=_value(f"New{NBSP}York"),
            params={"option": "New York"},
        )
    )
    assert isinstance(plan.subgoals[0].condition, FieldValue)


def test_a_value_whose_text_is_not_the_option_is_refused() -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan(condition=_value("Medium")))
    assert caught.value.path == "$.subgoals[0].condition"


def test_a_value_on_the_combobox_differing_in_case_is_the_chooses() -> None:
    value = plan_from_json(_plan(condition=_value("large"))).subgoals[0].condition
    assert isinstance(value, FieldValue)
    assert value.folded is True


def test_an_option_in_another_case_than_the_goal_is_verbatim() -> None:
    # PR #192 review: "set the size to large" chooses the option "Large"
    plan = plan_from_json(_plan(goal="set the size to large", condition=_value()))
    assert plan.steps[0].params["option"] == "Large"


def test_an_option_absent_from_the_goal_in_any_case_is_screen_content() -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan(goal="SET THE SIZE TO MEDIUM"))
    assert caught.value.path == "$.steps[0].params.option"
    assert "screen_content" in caught.value.reason


def test_a_typed_value_keeps_the_exact_comparator() -> None:
    body = {
        "goal": "type a b in the note",
        "subgoals": [
            {
                "id": "g1",
                "desc": "typed",
                "condition": {"value": {"role": "Edit", "name": "Note", "text": "a b"}},
            }
        ],
        "steps": [
            {
                "id": "s1",
                "subgoal": "g1",
                "verb": "type",
                "target": {"role": "Edit", "name": "Note"},
                "text": "a b",
            }
        ],
    }
    value = plan_from_json(json.dumps(body)).subgoals[0].condition
    assert isinstance(value, FieldValue)
    assert value.folded is False


def test_a_choose_value_subgoal_may_hold_an_r2_step() -> None:
    # W4 v1 verdict item 4: a value proven by a choose may tick after R2/R3, as a state does
    plan = plan_from_json(_plan(condition=_value(), risk="R2"))
    assert plan.steps[0].risk == "R2"


def test_a_typed_value_subgoal_still_refuses_an_r2_step() -> None:
    body = json.loads(_plan(condition=_value()))
    body["subgoals"][0]["condition"] = {"value": {"role": "Edit", "name": "Note", "text": "x"}}
    body["steps"].append(
        {
            "id": "s2",
            "subgoal": "g1",
            "verb": "type",
            "target": {"role": "Edit", "name": "Note"},
            "text": "x",
        }
    )
    body["steps"][0]["risk"] = "R2"
    with pytest.raises(PlanError, match="R2/R3"):
        plan_from_json(json.dumps(body))


def test_a_choose_value_subgoal_holds_no_submit() -> None:
    body = json.loads(_plan(condition=_value()))
    body["steps"].append(
        {
            "id": "s2",
            "subgoal": "g1",
            "verb": "submit",
            "target": {"role": "Button", "name": "Save"},
            "risk": "R2",
            "expect": {"appeared": {"role": "Text", "name": "Saved"}},
        }
    )
    with pytest.raises(PlanError, match="submit"):
        plan_from_json(json.dumps(body))


# ---- template and strength (W4-5, W4 v1 verdict ruling (c)) --------------------------------------


def test_the_template_is_the_comboboxs_folded_value() -> None:
    template = template_for("choose", Params(target_id=5, option="Large"))
    assert template == ValueEquals(target_id=5, text="Large", folded=True)


@pytest.mark.parametrize("tier", ["R2", "R3"])
def test_the_chooses_own_value_is_strong_at_r2_and_r3(tier: Any) -> None:
    params = Params(target_id=5, option="Large")
    own = ValueEquals(target_id=5, text="Large", folded=True)
    assert strength(own, tier, params, verb="choose").ok


def test_a_types_value_stays_weak_at_r2() -> None:
    params = Params(target_id=5, text="Large", option="Large")
    typed = ValueEquals(target_id=5, text="Large")
    assert strength(typed, "R2", params, verb="type").ok is False
    assert strength(typed, "R2", params, verb="choose").ok is False


def test_another_element_another_text_or_a_nested_value_is_not_strong() -> None:
    params = Params(target_id=5, option="Large")
    own = ValueEquals(target_id=5, text="Large", folded=True)
    assert strength(replace_text(own, "Small"), "R2", params, verb="choose").ok is False
    assert strength(ValueEquals(6, "Large", folded=True), "R2", params, verb="choose").ok is False
    assert strength(All((own, own)), "R2", params, verb="choose").ok is False
    assert strength(own, "R2", params, verb="click").ok is False
    assert strengthen(own, "click", "R2", params) is None


def replace_text(value: ValueEquals, text: str) -> ValueEquals:
    return ValueEquals(target_id=value.target_id, text=text, folded=value.folded)


def test_an_r1_choose_is_approved_with_its_template_unasked() -> None:
    confirmer = _Confirmer()
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    result = gate.decide(_step(), action_key="k", expect=None, target_pid=2, target_id=5)
    assert result.outcome == "approve", result.evidence
    assert result.expect == ValueEquals(target_id=5, text="Large", folded=True)
    assert confirmer.asked == []


def test_an_r2_choose_asks_and_the_ask_names_the_option() -> None:
    confirmer = _Confirmer()
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    step = _step("Premium ($20/mo)", name="Plan")
    result = gate.decide(step, action_key="k", expect=None, target_pid=2, target_id=5)
    assert result.outcome == "confirm", result.evidence
    assert confirmer.asked[0].risk == "R2"
    assert confirmer.asked[0].verb == "choose"
    assert confirmer.asked[0].params["option"] == "Premium ($20/mo)"


# ---- tier (W4-6, W4-03) --------------------------------------------------------------------------


def test_a_billed_option_reaches_r2() -> None:
    assert effective_risk(_step("Premium ($20/mo)", name="Plan")) == "R2"


def test_a_deny_word_in_the_option_reaches_r3() -> None:
    assert effective_risk(_step("Delete selected", name="Bulk action")) == "R3"


def test_a_deny_word_in_the_comboboxs_name_reaches_r3() -> None:
    assert effective_risk(_step("Now", name="Delete account")) == "R3"


def test_yes_under_a_consent_group_reaches_r2() -> None:
    step = _step("Yes", name="Answer")
    assert deny_tier(step, group_name="I consent to data sharing") == "R2"
    assert effective_risk(step, group_name="I consent to data sharing") == "R2"


@pytest.mark.parametrize("option", ["Subscribe", "I agree", "Monthly billing"])
def test_control_consent_and_billing_words_in_the_option_reach_r2(option: str) -> None:
    assert effective_risk(_step(option, name="Answer")) == "R2"


def test_a_plain_option_stays_r1() -> None:
    assert effective_risk(_step()) == "R1"


def test_the_option_is_not_read_for_another_verb_on_a_combobox() -> None:
    focus = Step(id="s1", subgoal="g1", verb="focus", target=Target(role="ComboBox", name="Size"))
    assert deny_tier(focus, group_name="I consent to data sharing") == "R0"


# ---- goal-match row (W4 v1 verdict item 5) -------------------------------------------------------


def test_the_goal_match_row_carries_the_option() -> None:
    _system, user = build_prompt(GOAL, [_step()], tag="t1")
    row = json.loads(next(line for line in user.splitlines() if line.startswith("{")))
    assert row == {"n": 1, "verb": "choose", "role": "ComboBox", "name": "Size", "option": "Large"}
    assert GOAL_MATCH_PROMPT_VERSION == "goal-match-2"
