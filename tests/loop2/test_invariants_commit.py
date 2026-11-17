"""Which subgoals are commits, the refusals before any ask, and the caution-R2 post-check
(packet v2-step4-claims §3.3; [V2.15] Q2, Q3, amendment 2), refreshed to [V2.88] L8:
`commit_undeclared` is a model fault only when the commit fact was on the row the model acted
on, else a limit. Red on the step 4 stub."""

from __future__ import annotations

import pytest

from steerai.loop2.claims import Sent
from steerai.loop2.consent import (
    WordClass,
    commit_refusal,
    effective_commit,
    needs_write_ahead,
    post_check,
    refusal_kind,
    tier_for,
)
from steerai.loop2.query import query_outcome
from steerai.loop2.task import Commit, GoalSpan
from steerai.orchestrator.contracts import Risk
from steerai.world.effects import EffectClass, Token
from tests.loop2.world_fakes import FIELD, GOAL, pending, report

TEXT = GoalSpan.of(GOAL, 20, 29)
"""`red shoes`, the one content param."""
TYPED = Sent(field=FIELD.runtime_id, span=TEXT, read_back=True)
"""The field's pending-text record holding the param, the digest read back equal."""


@pending("§3.3 the effective commit: declared, raised by dispatched actions", step=4)
def test_an_action_declared_send_raises_a_commit_none_subgoal() -> None:
    assert effective_commit("none", ["none", "send"]) == "send"


@pending("§3.3 the effective commit: never lowered", step=4)
def test_a_commit_none_action_never_lowers_a_declared_commit() -> None:
    assert effective_commit("submit", ["none"]) == "submit"


@pending("§3.3 the effective commit", step=4)
def test_nothing_declared_stays_none() -> None:
    assert effective_commit("none", []) == "none"


@pending("[V2.15] Q2: a commit subgoal is strict, any other standard", step=4)
@pytest.mark.parametrize(
    ("commit", "tier"), [("none", "standard"), ("send", "strict"), ("submit", "strict")]
)
def test_the_proof_tier_follows_the_effective_commit(commit: Commit, tier: str) -> None:
    assert tier_for(commit) == tier


def refusal(
    *,
    subgoal: Commit = "none",
    prior: tuple[Commit, ...] = (),
    action: Commit = "none",
    verb: str = "click",
    raised_by: WordClass | None = None,
    pending: Sent | None = TYPED,
) -> str | None:
    return commit_refusal(
        subgoal_commit=subgoal,
        prior=prior,
        action_commit=action,
        verb=verb,
        raised_by=raised_by,
        params={"text": TEXT},
        pending=pending,
    )


@pending("[V2.15] Q2 commit_undeclared: a deny-word raise needs a declared commit", step=4)
@pytest.mark.parametrize("verb", ["click", "press", "check"])
def test_a_deny_word_raise_with_commit_none_is_commit_undeclared(verb: str) -> None:
    assert refusal(verb=verb, raised_by="deny") == "commit_undeclared"


@pending("[V2.15] Q2: a deny-word raise with a declared commit goes on", step=4)
def test_a_deny_word_raise_with_a_declared_send_and_a_typed_param_is_allowed() -> None:
    assert refusal(raised_by="deny", action="send", subgoal="send") is None


@pending("[V2.15] Q2: only a deny-word raise makes a subgoal strict", step=4)
def test_a_caution_raise_without_a_word_class_is_not_refused() -> None:
    assert refusal(raised_by=None, action="none") is None


@pending(
    "[V2.17].1: a control, consent or billing raise on a click or a press is like a deny word",
    step=4,
)
@pytest.mark.parametrize("verb", ["click", "press"])
@pytest.mark.parametrize("word", ["control", "consent", "billing"])
def test_a_word_class_raise_on_a_click_or_press_needs_a_commit(verb: str, word: WordClass) -> None:
    assert refusal(verb=verb, raised_by=word) == "commit_undeclared"


@pending("[V2.17].1: on a state verb those classes ask, and the live read proves the state", step=4)
@pytest.mark.parametrize("verb", ["check", "uncheck", "select"])
@pytest.mark.parametrize("word", ["control", "consent", "billing"])
def test_a_word_class_raise_on_a_state_verb_is_not_refused(verb: str, word: WordClass) -> None:
    assert refusal(verb=verb, raised_by=word) is None


@pending("§3.3 commit_conflict", step=4)
def test_two_commit_kinds_on_one_subgoal_are_commit_conflict() -> None:
    assert refusal(subgoal="send", action="submit") == "commit_conflict"


@pending("§3.3 commit_conflict: against an earlier dispatched action", step=4)
def test_a_kind_other_than_an_earlier_actions_is_commit_conflict() -> None:
    assert refusal(prior=("send",), action="submit") == "commit_conflict"


@pending("[V2.15] Q3: delete and pay are not in 4a to 4d", step=4)
@pytest.mark.parametrize("commit", ["delete", "pay"])
def test_delete_and_pay_are_not_in_step(commit: Commit) -> None:
    assert refusal(subgoal=commit, action=commit, raised_by="deny") == "not_in_step"


@pending("[V2.15] amendment 2, [V2.88] L3: no_strict_shape is a recorded limit", step=4)
@pytest.mark.parametrize("commit", ["send", "submit"])
def test_a_send_without_a_pending_text_record_is_no_strict_shape(commit: Commit) -> None:
    assert refusal(subgoal=commit, action=commit, raised_by="deny", pending=None) == (
        "no_strict_shape"
    )


@pending("[V2.88] L8: commit_undeclared is a model fault when the row showed the fact", step=4)
def test_commit_undeclared_with_the_commit_fact_on_the_acted_row_is_a_model_fault() -> None:
    assert refusal_kind("commit_undeclared", commit_on_row=True) == "model"


@pending("[V2.88] L8: commit_undeclared is a limit, no strike, when the row did not", step=4)
def test_commit_undeclared_without_the_commit_fact_on_the_acted_row_is_a_limit() -> None:
    assert refusal_kind("commit_undeclared", commit_on_row=False) == "step"


@pending("§8.4: every R2/R3 dispatch has a write-ahead record first", step=4)
@pytest.mark.parametrize(
    ("risk", "needed"), [("R0", False), ("R1", False), ("R2", True), ("R3", True)]
)
def test_r2_and_r3_need_a_write_ahead_record(risk: Risk, needed: bool) -> None:
    assert needs_write_ahead(risk) is needed


@pending(
    "[V2.15] Q2: a caution-R2 Enter under a commit-none subgoal takes the query outcomes", step=4
)
def test_a_caution_r2_enter_takes_the_three_outcome_post_check() -> None:
    assert post_check(risk="R2", subgoal_commit="none", classified=True) == "query_outcome"


@pending("§7.2: an R1 query takes the query outcomes", step=4)
def test_an_r1_query_takes_the_three_outcome_post_check() -> None:
    assert post_check(risk="R1", subgoal_commit="none", classified=True) == "query_outcome"


@pending("§8.8: an action under a commit subgoal waits for a strict claim", step=4)
def test_a_send_waits_for_a_strict_claim() -> None:
    assert post_check(risk="R2", subgoal_commit="send", classified=False) == "strict_claim"


@pending("§3.3: an unclassified click has no post-check", step=4)
def test_a_plain_click_has_no_post_check() -> None:
    assert post_check(risk="R0", subgoal_commit="none", classified=False) == "none"


def test_a_navigation_after_a_caution_enter_is_expected() -> None:
    assert query_outcome(report("navigated"), text_carried=True).outcome == "expected"


def test_results_carrying_the_text_after_a_caution_enter_are_expected() -> None:
    assert query_outcome(report("content_changed"), text_carried=True).outcome == "expected"


@pytest.mark.parametrize("effect", ["dialog_opened", "revealed"])
def test_a_dialog_or_reveal_with_no_carrier_after_a_caution_enter_is_unexpected(
    effect: EffectClass,
) -> None:
    """[V2.21] row 4 (a revealed list carrying the text is row 6, expected)."""
    assert query_outcome(report(effect), text_carried=False).outcome == "unexpected_effect"


def test_a_change_without_the_text_after_a_caution_enter_is_unexpected() -> None:
    outcome = query_outcome(report("content_changed"), text_carried=False)
    assert outcome.outcome == "unexpected_effect"


WEAK: list[tuple[tuple[EffectClass, ...], tuple[Token, ...]]] = [
    (("focus",), ()),
    (("own_state",), ()),
    ((), ("frame_changed",)),
    (("focus", "own_state"), ("frame_changed",)),
]


@pytest.mark.parametrize(("classes", "tokens"), WEAK, ids=["focus", "own_state", "frame", "all"])
def test_weak_only_after_a_caution_enter_goes_back_as_weak(
    classes: tuple[EffectClass, ...], tokens: tuple[Token, ...]
) -> None:
    """[V2.21] row 8: back to the controller with its own token, and the key stays consumed."""
    outcome = query_outcome(report(*classes, tokens=tokens), text_carried=False)
    assert (outcome.outcome, outcome.token) == ("back_to_controller", "weak")
    assert outcome.releases_key is False


def test_unknown_after_a_caution_enter_goes_back_as_unknown() -> None:
    """[V2.21] row 5: unknown is its own token now, never weak; the key stays consumed."""
    outcome = query_outcome(report("unknown"), text_carried=False)
    assert (outcome.outcome, outcome.token) == ("back_to_controller", "unknown")
    assert outcome.releases_key is False
