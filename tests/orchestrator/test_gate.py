"""The safety gate in bootstrap mode (design 5.13, [v0.4] bootstrap, orchestrator review
OR-13/14/15/18/19/28): R0/R1 approve with strict T1, R2/R3 confirm through a bound, single-use
decision; deny patterns raise the tier; blocks are final."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import (
    DENY_PATTERNS,
    Ask,
    Gate,
    GateResult,
    effective_risk,
)
from steerai.verify.predicates import Appeared, Focused, Within
from steerai.verify.strength import Params

CONVERSATION = Within(landmark="Conversation")
STRONG = Appeared(role="Text", name_pattern="hello there", within=CONVERSATION)


@dataclass
class FakeConfirmer:
    """Answers asks by decision id; records what was asked; can answer late or not at all."""

    answers: dict[str, str] = field(default_factory=dict)
    default: str = "unanswered"
    asked: list[Ask] = field(default_factory=list)

    def ask(self, ask: Ask) -> str:
        self.asked.append(ask)
        return self.answers.get(ask.decision_id, self.default)


def _step(verb: str = "click", risk: str = "R1", name: str = "Chat options", **kw: Any) -> Step:
    # The content predicate's pattern must be a step parameter (5.4 [v0.5]).
    kw.setdefault("params", {"text": "hello there"})
    return Step(
        id="s1",
        subgoal="g1",
        verb=verb,
        target=Target(role="Button", name=name),
        risk=risk,  # type: ignore[arg-type]
        **kw,
    )


def _gate(confirmer: FakeConfirmer | None = None, own_pid: int = 4242) -> Gate:
    return Gate(confirmer=confirmer or FakeConfirmer(), own_pid=own_pid, task_id="task-1")


def test_deny_patterns_raise_the_tier_never_lower_it() -> None:
    assert effective_risk(_step(risk="R1", name="Delete chat")) == "R3"
    assert effective_risk(_step(risk="R0", name="Send")) == "R2"
    assert effective_risk(_step(risk="R0", name="Pay now")) == "R3"
    assert effective_risk(_step(risk="R3", name="Chat options")) == "R3"
    assert effective_risk(_step(risk="R1", name="Chat options")) == "R1"
    assert all(word == word.casefold() for word in DENY_PATTERNS)


def test_r0_and_r1_are_approved_with_a_bound_single_use_decision() -> None:
    gate = _gate()
    result = gate.decide(_step(), action_key="k-1", expect=STRONG, target_pid=777)
    assert isinstance(result, GateResult)
    assert result.outcome == "approve"
    assert result.decision is not None
    assert result.decision.action_key == "k-1"
    assert result.decision.consume("k-1") is True
    assert result.decision.consume("k-1") is False


def test_r2_and_r3_always_confirm_in_bootstrap_mode_even_when_preapproved() -> None:
    confirmer = FakeConfirmer(default="yes")
    gate = _gate(confirmer)
    step = _step(verb="send", name="Send", risk="R2", preapproved_by_request=True)
    result = gate.decide(step, action_key="k-send", expect=STRONG, target_pid=777)
    assert result.outcome == "confirm"
    assert result.decision is not None
    assert result.decision.outcome == "confirm"
    assert len(confirmer.asked) == 1
    ask = confirmer.asked[0]
    assert ask.action_key == "k-send"
    assert ask.decision_id == result.decision.decision_id
    assert ask.risk == "R2"
    assert "Send" not in repr(ask), "names stay out of the repr"


def test_a_no_blocks_and_an_unanswered_ask_pauses() -> None:
    step = _step(verb="send", name="Send", risk="R2")
    said_no = _gate(FakeConfirmer(default="no")).decide(
        step, action_key="k", expect=STRONG, target_pid=777
    )
    assert said_no.outcome == "block"
    assert said_no.code is ErrorCode.GATE_BLOCKED
    assert said_no.decision is None
    silent = _gate(FakeConfirmer(default="unanswered")).decide(
        step, action_key="k", expect=STRONG, target_pid=777
    )
    assert silent.outcome == "pending"
    assert silent.code is ErrorCode.GATE_PENDING_USER
    assert silent.decision is None


def test_a_late_answer_for_another_decision_is_discarded() -> None:
    # OR-13: an answer is accepted only for the open ask's decision id.
    class Stale(FakeConfirmer):
        def ask(self, ask: Ask) -> str:
            self.asked.append(ask)
            return f"yes:{'d-stale'}"

    result = _gate(Stale()).decide(
        _step(verb="send", name="Send", risk="R2"), action_key="k", expect=STRONG, target_pid=777
    )
    assert result.outcome == "pending"


def test_the_answer_may_carry_its_decision_id() -> None:
    class Bound(FakeConfirmer):
        def ask(self, ask: Ask) -> str:
            self.asked.append(ask)
            return f"yes:{ask.decision_id}"

    result = _gate(Bound()).decide(
        _step(verb="send", name="Send", risk="R2"), action_key="k", expect=STRONG, target_pid=777
    )
    assert result.outcome == "confirm"


def test_weak_predicate_at_r2_is_blocked_before_any_ask() -> None:
    confirmer = FakeConfirmer(default="yes")
    result = _gate(confirmer).decide(
        _step(verb="send", name="Send", risk="R2"),
        action_key="k",
        expect=Focused(target_id=1),
        target_pid=777,
    )
    assert result.outcome == "block"
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "weak_predicate"
    assert result.evidence["reason"] == "focused_alone"
    assert confirmer.asked == []


def test_weak_predicate_at_r1_is_also_blocked_strict_t1_means_no_weak_checks() -> None:
    result = _gate().decide(
        _step(risk="R1"), action_key="k", expect=Focused(target_id=1), target_pid=777
    )
    assert result.outcome == "block"
    assert result.evidence["detail"] == "weak_predicate"


def test_missing_predicate_is_blocked() -> None:
    result = _gate().decide(_step(risk="R1"), action_key="k", expect=None, target_pid=777)
    assert result.outcome == "block"
    assert result.evidence["detail"] == "no_predicate"


def test_own_process_is_blocked_before_everything() -> None:
    confirmer = FakeConfirmer(default="yes")
    result = _gate(confirmer, own_pid=777).decide(
        _step(verb="send", name="Send", risk="R2"), action_key="k", expect=STRONG, target_pid=777
    )
    assert result.outcome == "block"
    assert result.evidence["detail"] == "self_target"
    assert confirmer.asked == []


def test_a_block_is_final_no_decision_object_exists() -> None:
    result = _gate().decide(_step(risk="R1"), action_key="k", expect=None, target_pid=777)
    assert result.decision is None


def test_ask_text_is_built_from_tokens_the_confirmer_renders_itself() -> None:
    confirmer = FakeConfirmer(default="yes")
    _gate(confirmer).decide(
        _step(verb="send", name="Send", risk="R2", params={"text": "hunter2"}),
        action_key="k",
        expect=Appeared(role="Text", name_pattern="hunter2", within=CONVERSATION),
        target_pid=777,
        previous_attempt="delivery_uncertain",
    )
    ask = confirmer.asked[0]
    assert ask.verb == "send"
    assert ask.target_role == "Button"
    assert ask.previous_attempt == "delivery_uncertain"
    assert ask.task_id == "task-1"
    assert "hunter2" not in repr(ask)
    assert ask.params["text"] == "hunter2", "the confirmer shows the full text to the user"


def test_decision_ids_are_unique_per_ask() -> None:
    confirmer = FakeConfirmer(default="yes")
    gate = _gate(confirmer)
    step = _step(verb="send", name="Send", risk="R2")
    first = gate.decide(step, action_key="k", expect=STRONG, target_pid=777)
    second = gate.decide(step, action_key="k", expect=STRONG, target_pid=777)
    assert first.decision is not None
    assert second.decision is not None
    assert first.decision.decision_id != second.decision.decision_id


def test_strength_params_come_from_the_step_not_the_model() -> None:
    # The gate strengthens from the step's own parameters (code-filled, 5.4 [v0.5]).
    gate = _gate(FakeConfirmer(default="yes"))
    step = Step(
        id="s1",
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        risk="R2",
        params={"text": "hello there", "composer_id": "12", "scope": "Conversation"},
    )
    result = gate.decide(step, action_key="k", expect=None, target_pid=777)
    assert result.outcome == "confirm", result.evidence
    assert result.expect is not None
    assert isinstance(result.params, Params)


@pytest.mark.parametrize("answer", ["YES", " yes ", "y"])
def test_only_an_exact_yes_counts(answer: str) -> None:
    # OR-14: a bare Enter or a sloppy token is not consent; the confirmer returns a token.
    result = _gate(FakeConfirmer(default=answer)).decide(
        _step(verb="send", name="Send", risk="R2"), action_key="k", expect=STRONG, target_pid=777
    )
    assert result.outcome == "pending"
