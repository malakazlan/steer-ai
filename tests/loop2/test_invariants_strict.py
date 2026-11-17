"""The strict tier for commit subgoals, bound to the write-ahead record (V2 §8.8; packet
v2-step4-claims §3.3; [V2.15] Q1), refreshed to [V2.88]: one content param (L2), and the binding
to the content field's pending-text record by provenance and digest, never by the subgoal label
on the `type` (L3). Red on the step 4 stub."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

import pytest

from steerai.loop2.claims import (
    T1_REASONS,
    Checked,
    CommitRecord,
    Proof,
    Sent,
    SubgoalFacts,
    Tier,
    check_content,
    closes,
    strict_reason,
)
from steerai.loop2.consent import commit_refusal
from steerai.loop2.decision import Claim
from steerai.loop2.runner import REFUSAL_KIND
from steerai.loop2.task import Commit, GoalSpan
from steerai.world.elements import Element
from tests.loop2.world_fakes import element, pending, snapshot

REQUEST = "reply ok thanks to the team"
CONTENT = GoalSpan.of(REQUEST, 6, 15)
"""`ok thanks`."""
OTHER = GoalSpan.of(REQUEST, 19, 27)
"""`the team`."""
DOC = element(1, "Document", "Messages", (5, 1), None)
COMPOSER = element(2, "Edit", "Message", (5, 2), 1)
SEND = element(3, "Button", "Send", (5, 3), 1)
CONVERSATION = element(4, "List", "Conversation", (5, 4), 1)
PAGE = [DOC, COMPOSER, SEND, CONVERSATION]
BUBBLE = element(10, "ListItem", "ok thanks", (5, 10), 4)
BEFORE = snapshot(PAGE)
RECORD = CommitRecord(action_key="k-send", subgoal="s1", phase="acting", before=BEFORE)
SENT = Sent(field=COMPOSER.runtime_id, span=CONTENT, read_back=True)
"""The composer's pending-text record: typed from the content span, the digest read back equal."""
SUBGOAL = SubgoalFacts(
    params={"text": CONTENT},
    commit="send",
    baseline=BEFORE,
    typed_fields=(COMPOSER.runtime_id,),
    typed_words=frozenset({"ok", "thanks"}),
)
"""Steer typed the content itself, so every word of it is typed: [V2.91]'s typed-words-only
rule is the standard tier's; the strict tier proves Steer's own text by equality instead."""


def claim(*ids: int, param: str = "text") -> Claim:
    return Claim(subgoal="s1", kind="content_present", evidence=list(ids), param=param)


def check(
    after: Sequence[Element],
    *,
    cited: Claim | None = None,
    records: Sequence[CommitRecord] = (RECORD,),
    sent: Sent | None = SENT,
    filled: bool | None = False,
    subgoal: SubgoalFacts = SUBGOAL,
) -> Checked:
    return check_content(
        cited or claim(BUBBLE.id),
        snapshot(after),
        subgoal,
        scope_document=DOC.runtime_id,
        records=records,
        sent=sent,
        field_filled=filled,
    )


@pending("§8.8 strict content_present bound to the write-ahead record", step=4)
def test_one_new_element_holding_the_whole_content_is_a_strict_proof() -> None:
    result = check([*PAGE, BUBBLE])
    assert result.tokens == ()
    assert result.proof is not None
    assert (result.proof.tier, result.proof.preexisting) == ("strict", False)


@pending("§8.8 the proof's action_id is the record's action key", step=4)
def test_the_strict_proof_is_bound_to_the_records_action_key() -> None:
    result = check([*PAGE, BUBBLE])
    assert result.proof is not None
    assert result.proof.action_id == RECORD.action_key


@pending("§8.8 no_record", step=4)
def test_without_a_record_there_is_no_strict_proof() -> None:
    assert "no_record" in check([*PAGE, BUBBLE], records=()).tokens


@pending("§8.8 no_record: a record only written, never dispatched", step=4)
def test_a_record_still_in_phase_written_is_no_record() -> None:
    written = dataclasses.replace(RECORD, phase="written")
    assert "no_record" in check([*PAGE, BUBBLE], records=(written,)).tokens


@pending("[V2.15] Q1: the record binds the proof by the subgoal id it holds", step=4)
def test_a_record_of_another_subgoal_is_no_record() -> None:
    other = dataclasses.replace(RECORD, subgoal="s2")
    assert "no_record" in check([*PAGE, BUBBLE], records=(other,)).tokens


@pending("[V2.88] L3: param_not_sent binds to the record's provenance and digest", step=4)
@pytest.mark.parametrize(
    "record",
    [
        dataclasses.replace(SENT, span=OTHER),
        dataclasses.replace(SENT, span=None),
        dataclasses.replace(SENT, read_back=False),
    ],
    ids=["another_span", "generated_text", "digest_differs"],
)
def test_a_record_not_holding_this_subgoals_param_is_param_not_sent(record: Sent) -> None:
    result = check([*PAGE, BUBBLE], sent=record)
    assert result.proof is None
    assert "param_not_sent" in result.tokens


@pending("[V2.88] L3: a missing or unread pending-text record fails closed", step=4)
@pytest.mark.parametrize(
    "record", [None, dataclasses.replace(SENT, read_back=None)], ids=["missing", "unread"]
)
def test_a_missing_or_unread_record_fails_closed(record: Sent | None) -> None:
    result = check([*PAGE, BUBBLE], sent=record)
    assert result.proof is None
    assert "param_not_sent" in result.tokens


def test_the_pending_text_record_carries_no_subgoal_label() -> None:
    """[V2.88] L3, [V2.72]: the label on the `type` is ignored, so the record the strict tier
    binds to has no field for it."""
    assert {f.name for f in dataclasses.fields(Sent)} == {"field", "span", "read_back"}


def refusal(
    *, commit: Commit = "send", params: Mapping[str, GoalSpan] | None = None, pending: Sent | None
) -> str | None:
    return commit_refusal(
        subgoal_commit=commit,
        prior=(),
        action_commit=commit,
        verb="click",
        raised_by="deny",
        params=SUBGOAL.params if params is None else params,
        pending=pending,
    )


@pytest.mark.parametrize("commit", ["send", "submit"])
def test_a_commit_with_one_param_and_its_record_is_not_refused(commit: Commit) -> None:
    assert refusal(commit=commit, pending=SENT) is None


@pytest.mark.parametrize("commit", ["send", "submit"])
@pytest.mark.parametrize(
    "params", [{}, {"text": CONTENT, "to": OTHER}], ids=["no_param", "two_params"]
)
def test_a_commit_subgoal_without_exactly_one_param_is_no_strict_shape(
    commit: Commit, params: Mapping[str, GoalSpan]
) -> None:
    assert refusal(commit=commit, params=params, pending=SENT) == "no_strict_shape"


@pytest.mark.parametrize(
    "record",
    [None, dataclasses.replace(SENT, span=OTHER), dataclasses.replace(SENT, span=None)],
    ids=["missing", "another_span", "generated_text"],
)
def test_a_send_whose_record_does_not_hold_the_param_is_send_not_typed(
    record: Sent | None,
) -> None:
    assert refusal(pending=record) == "send_not_typed"


def test_no_strict_shape_is_a_step_limit_and_send_not_typed_a_model_fault() -> None:
    """[V2.90] (1): `no_strict_shape` keeps one kind, a step limit with no strike, for a declared
    shape beyond step 4; a send whose content Steer did not type is the model's fault."""
    assert REFUSAL_KIND["no_strict_shape"] == "step"
    assert REFUSAL_KIND["send_not_typed"] == "model"


@pytest.mark.parametrize(
    ("read_back", "token"),
    [(False, "composer_differs"), (None, "composer_unreadable")],
    ids=["digest_differs", "unread"],
)
def test_a_failed_read_back_of_the_record_is_a_world_refusal(
    read_back: bool | None, token: str
) -> None:
    """The record holds the param's span, but its digest read back differs or was never read;
    nothing is sent in either case."""
    assert refusal(pending=dataclasses.replace(SENT, read_back=read_back)) == token
    assert REFUSAL_KIND[token] == "world"


@pending("§3.3 strict rule 4, T1 not_new ([V2.17].7)", step=4)
def test_content_already_in_the_records_before_is_not_new() -> None:
    old = dataclasses.replace(RECORD, before=snapshot([*PAGE, BUBBLE]))
    assert "not_new" in check([*PAGE, BUBBLE], records=(old,)).tokens


@pending("[V2.90] (2): a strict send's evidence must be new after the dispatch", step=4)
def test_a_live_preview_shown_before_the_dispatch_never_proves_the_send() -> None:
    """A preview of the draft, outside the composer, already held the words on the observation
    before the dispatch (the record's before). It appeared after the task's first observation,
    so the standard delta would count it; the strict tier never does."""
    preview = element(15, "Text", "ok thanks", (5, 15), 1)
    record = dataclasses.replace(RECORD, before=snapshot([*PAGE, preview]))
    result = check([*PAGE, preview], cited=claim(preview.id), records=(record,))
    assert result.proof is None
    assert "not_new" in result.tokens


@pending("[V2.91]: a strict send's new element's name equals the typed text, folded", step=4)
@pytest.mark.parametrize("name", ["ok thanks", "Ok thanks", "ok  thanks"])
def test_an_honest_send_whose_name_equals_the_typed_text_is_a_strict_proof(name: str) -> None:
    bubble = element(16, "ListItem", name, (5, 16), 4)
    result = check([*PAGE, bubble], cited=claim(bubble.id))
    assert result.proof is not None
    assert "typed_words_only" not in result.tokens


@pending("[V2.91], #511 must-not-prove: a name holding the typed text is not equal", step=4)
@pytest.mark.parametrize(
    "name", ["Failed to send: ok thanks", "No results for ok thanks"], ids=["failed", "no_results"]
)
def test_a_name_that_only_holds_the_typed_text_never_proves_the_send(name: str) -> None:
    """T1's `name_matches` is full-string (VF-06), so the template gives `no_match`."""
    line = element(17, "ListItem", name, (5, 17), 4)
    result = check([*PAGE, line], cited=claim(line.id))
    assert result.proof is None
    assert "no_match" in result.tokens


@pending("§3.3 strict rule 4 duplicate_effect", step=4)
def test_two_new_matching_elements_are_a_duplicate_effect() -> None:
    twin = element(11, "ListItem", "ok thanks", (5, 11), 4)
    assert "duplicate_effect" in check([*PAGE, BUBBLE, twin]).tokens


@pending("§3.3 strict tier: the earliest record's before is used", step=4)
def test_a_second_record_never_hides_a_duplicate() -> None:
    later = CommitRecord(
        action_key="k-send", subgoal="s1", phase="acting", before=snapshot([*PAGE, BUBBLE])
    )
    twin = element(11, "ListItem", "ok thanks", (5, 11), 4)
    assert "duplicate_effect" in check([*PAGE, BUBBLE, twin], records=(RECORD, later)).tokens


@pending("§3.3 strict rule 4, T1 no_match ([V2.17].7)", step=4)
def test_part_of_the_content_is_no_match() -> None:
    part = element(12, "ListItem", "ok", (5, 12), 4)
    assert "no_match" in check([*PAGE, part], cited=claim(part.id)).tokens


@pending("§3.3 inside_composer, a v2 token with no T1 equivalent", step=4)
def test_the_content_inside_the_composer_is_inside_composer() -> None:
    echo = element(13, "Text", "ok thanks", (5, 13), 2)
    assert "inside_composer" in check([*PAGE, echo], cited=claim(echo.id)).tokens


@pending("§3.3 composer_not_cleared, a v2 token with no T1 equivalent", step=4)
def test_a_field_still_filled_is_composer_not_cleared() -> None:
    assert "composer_not_cleared" in check([*PAGE, BUBBLE], filled=True).tokens


@pending("§3.3 strict rule 6 composer_unreadable", step=4)
def test_an_unread_field_is_composer_unreadable() -> None:
    assert "composer_unreadable" in check([*PAGE, BUBBLE], filled=None).tokens


@pending("§3.3 strict rule 6: a field that is gone needs no read", step=4)
def test_a_composer_that_is_gone_passes_without_a_read() -> None:
    gone = [DOC, SEND, CONVERSATION, BUBBLE]
    assert check(gone, filled=None).proof is not None


@pending("§3.3 strict rule 1: the scope rule holds at the strict tier", step=4)
def test_content_outside_the_scope_document_is_outside_scope() -> None:
    toast = element(14, "Text", "ok thanks", (5, 14), None)
    assert "outside_scope" in check([*PAGE, toast], cited=claim(toast.id)).tokens


def proof(tier: Tier) -> Proof:
    return Proof(
        claim=claim(BUBBLE.id),
        tier=tier,
        signals=frozenset(),
        preexisting=False,
        action_id="k-send" if tier == "strict" else None,
    )


@pending("§7.1, §8.8 standard_only: a standard proof never closes a commit subgoal", step=4)
def test_a_standard_proof_never_closes_a_commit_subgoal() -> None:
    assert closes("send", proof("standard")) == "standard_only"
    assert closes("submit", proof("standard")) == "standard_only"


@pending("§8.8: a strict proof closes a commit subgoal", step=4)
def test_a_strict_proof_closes_a_commit_subgoal() -> None:
    assert closes("send", proof("strict")) is None


@pending("§7.1: a standard proof closes a commit-none subgoal", step=4)
def test_a_standard_proof_closes_a_commit_none_subgoal() -> None:
    assert closes("none", proof("standard")) is None


@pending("[V2.17].7: the strict tier passes T1's Appeared reasons through unchanged", step=4)
@pytest.mark.parametrize("reason", T1_REASONS)
def test_every_t1_reason_passes_through_the_strict_tier(reason: str) -> None:
    assert strict_reason(reason) == reason


@pending("[V2.17].7: a reason T1 never gives is refused, never mapped", step=4)
def test_a_reason_outside_t1_is_refused() -> None:
    with pytest.raises(ValueError, match="reason"):
        strict_reason("preexisting")
