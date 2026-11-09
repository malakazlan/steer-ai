"""Orchestrator contracts (design 6.3 to 6.5, principal schema gate): a closed verb vocabulary,
validated steps and plans, a redacted step result, a single-use gate decision."""

from __future__ import annotations

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import (
    VERBS,
    GateDecision,
    Plan,
    Step,
    StepResult,
    Subgoal,
    Target,
    executor_verb,
    template_verb,
)
from steerai.verify.predicates import Appeared, Within


def _step(**overrides: object) -> Step:
    base: dict[str, object] = {
        "id": "s1",
        "subgoal": "g1",
        "verb": "type",
        "target": Target(role="Edit", name="Message"),
        "text": "hello there",
        "risk": "R1",
    }
    base.update(overrides)
    return Step(**base)  # type: ignore[arg-type]


# ---- verbs: a closed table mapping to the executors and to the verifier templates ---------------


def test_verb_table_is_closed_and_maps_to_executors_and_templates() -> None:
    assert (
        frozenset(
            {
                "click",
                "focus",
                "type",
                "send",
                "submit",
                "delete",
                "open",
                "check",
                "uncheck",
                "select",
                "choose",
            }
        )
        == VERBS
    )
    assert executor_verb("focus") == ("physical", "click")
    assert executor_verb("click") == ("semantic", "click")
    assert executor_verb("type") == ("physical", "type")
    assert executor_verb("send") == ("semantic", "click")
    assert executor_verb("submit") == ("semantic", "click")
    assert executor_verb("delete") == ("semantic", "click")
    assert executor_verb("check") == ("semantic", "check")
    assert executor_verb("uncheck") == ("semantic", "uncheck")
    assert executor_verb("select") == ("semantic", "select")
    assert executor_verb("choose") == ("semantic", "choose")
    assert template_verb("send") == "send"
    assert template_verb("click") == "click"
    with pytest.raises(ValueError, match="unknown verb"):
        executor_verb("teleport")


def test_open_has_no_executor_verb_yet() -> None:
    with pytest.raises(ValueError, match="open"):
        executor_verb("open")


# ---- steps and plans ----------------------------------------------------------------------------


def test_step_validates_verb_risk_and_parameters() -> None:
    assert _step().risk == "R1"
    with pytest.raises(ValueError, match="unknown verb"):
        _step(verb="teleport")
    with pytest.raises(ValueError, match="risk"):
        _step(risk="R4")
    with pytest.raises(ValueError, match="text"):
        _step(verb="type", text=None)
    with pytest.raises(ValueError, match="text"):
        _step(verb="click", text="stray")
    with pytest.raises(ValueError, match="target"):
        _step(target=None)


@pytest.mark.parametrize("role", ["Edit", "ComboBox", "Document"])
def test_a_focus_step_targets_a_field(role: str) -> None:
    assert _step(verb="focus", target=Target(role=role, name="Message"), text=None).verb == "focus"


@pytest.mark.parametrize("role", ["Button", "Hyperlink", "ListItem", "MenuItem", "Custom", "Pane"])
def test_a_focus_step_on_anything_but_a_field_is_refused(role: str) -> None:
    """IJ-02 (#82): `focus` is a physical click, so a focus on a button would press it at R0."""
    with pytest.raises(ValueError, match="focus targets a field"):
        _step(verb="focus", target=Target(role=role, name="Send"), text=None, risk="R0")


def test_a_focus_on_a_person_row_is_refused() -> None:
    with pytest.raises(ValueError, match="focus targets a field"):
        _step(verb="focus", target=Target(role="ListItem", name="Amir", match="person"), text=None)


def test_step_text_is_out_of_the_repr() -> None:
    assert "hello there" not in repr(_step())


def test_target_is_a_role_and_a_plain_name_or_a_scripted_element_id() -> None:
    assert Target(role="Edit", name="Message").element_id is None
    assert Target(role="Edit", name="Message", element_id=12).element_id == 12
    with pytest.raises(ValueError, match="role"):
        Target(role="", name="x")
    with pytest.raises(ValueError, match="name"):
        Target(role="Edit", name=" ")
    assert "Amir" not in repr(Target(role="ListItem", name="Amir"))


def test_send_is_always_at_least_r2_whatever_the_plan_says() -> None:
    step = _step(verb="send", text=None, target=Target(role="Button", name="Send"), risk="R1")
    assert step.risk == "R2"
    delete = _step(
        verb="delete", text=None, target=Target(role="Button", name="Delete chat"), risk="R0"
    )
    assert delete.risk == "R3"


def test_plan_needs_subgoals_with_conditions_and_steps_that_reference_them() -> None:
    condition = Appeared(
        role="Text", name_pattern="hello there", within=Within(landmark="Conversation")
    )
    plan = Plan(
        goal="send hello there to Amir",
        subgoals=(Subgoal(id="g1", desc="hello sent", condition=condition),),
        steps=(_step(),),
    )
    assert plan.steps[0].subgoal == "g1"
    with pytest.raises(ValueError, match="subgoal"):
        Plan(goal="g", subgoals=(), steps=(_step(),))
    with pytest.raises(ValueError, match="subgoal"):
        Plan(
            goal="g",
            subgoals=(Subgoal(id="g1", desc="d", condition=condition),),
            steps=(_step(subgoal="g9"),),
        )
    with pytest.raises(ValueError, match="duplicate"):
        Plan(
            goal="g",
            subgoals=(Subgoal(id="g1", desc="d", condition=condition),),
            steps=(_step(), _step()),
        )
    assert "send hello there" not in repr(plan)


def test_preapproved_by_request_needs_verbatim_text_provenance() -> None:
    step = _step(
        verb="send",
        text=None,
        target=Target(role="Button", name="Send"),
        preapproved_by_request=True,
    )
    assert step.preapproved_by_request is True
    with pytest.raises(ValueError, match="verbatim"):
        _step(preapproved_by_request=True, text_provenance="generated")


# ---- gate decisions and step results ---------------------------------------------------------


def test_gate_decision_is_single_use_and_bound_to_an_action_key() -> None:
    decision = GateDecision(decision_id="d-1", action_key="k-1", outcome="approve")
    assert decision.consume("k-1") is True
    assert decision.consume("k-1") is False, "single use"
    fresh = GateDecision(decision_id="d-2", action_key="k-1", outcome="confirm")
    assert fresh.consume("k-2") is False, "bound to its key"
    with pytest.raises(ValueError, match="outcome"):
        GateDecision(decision_id="d-3", action_key="k", outcome="maybe")  # type: ignore[arg-type]


def test_step_result_is_redacted_and_carries_stage_latencies() -> None:
    action = ActionResult(
        verb="click",
        delivery=Delivery.DELIVERED_VERIFIED,
        steps=[ActionStep(label="InvokePattern.Invoke", mechanism="semantic", outcome="succeeded")],
    )
    result = StepResult(
        step_id="s1",
        ok=True,
        gate="approve",
        action=action,
        verify_code=None,
        label_kind="t1",
        latency_ms={"observe": 25.0, "decide": 0.0, "gate": 0.2, "act": 21.0, "verify": 1.0},
        evidence={"reason": "no_postcondition"},
    )
    payload = result.to_dict()
    assert payload["step_id"] == "s1"
    assert payload["action"]["delivery"] == "delivered_verified"
    assert payload["latency_ms"]["act"] == 21.0
    with pytest.raises(ValueError, match="ok"):
        StepResult(
            step_id="s1",
            ok=True,
            gate="approve",
            action=action,
            verify_code=ErrorCode.VERIFY_TIMEOUT,
            label_kind=None,
            latency_ms={},
            evidence={},
        )


def test_step_path_override_is_validated() -> None:
    assert _step(verb="click", text=None, path="physical").path == "physical"
    with pytest.raises(ValueError, match="path"):
        _step(verb="click", text=None, path="teleport")


def test_a_focus_step_never_runs_on_the_semantic_path() -> None:
    """FC-09 (#82): plan_io refuses it; a step built in code must not get past the contract."""
    assert _step(verb="focus", text=None, path="physical").path == "physical"
    with pytest.raises(ValueError, match="focus runs on the physical path"):
        _step(verb="focus", text=None, path="semantic")
