"""The strict tier for commit subgoals, bound to the write-ahead record (V2 §8.8; packet
v2-step4-claims §3.3; [V2.15] Q1). Red on the step 4 stub."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

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
from steerai.loop2.decision import Claim
from steerai.loop2.task import GoalSpan
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
SENT = Sent(param="text", field=COMPOSER.runtime_id)
SUBGOAL = SubgoalFacts(
    params={"text": CONTENT, "to": OTHER},
    commit="send",
    baseline=BEFORE,
    typed_fields=(COMPOSER.runtime_id,),
)


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


@pending("§8.8 strict content_present bound to the write-ahead record")
def test_one_new_element_holding_the_whole_content_is_a_strict_proof() -> None:
    result = check([*PAGE, BUBBLE])
    assert result.tokens == ()
    assert result.proof is not None
    assert (result.proof.tier, result.proof.preexisting) == ("strict", False)


@pending("§8.8 the proof's action_id is the record's action key")
def test_the_strict_proof_is_bound_to_the_records_action_key() -> None:
    result = check([*PAGE, BUBBLE])
    assert result.proof is not None
    assert result.proof.action_id == RECORD.action_key


@pending("§8.8 no_record")
def test_without_a_record_there_is_no_strict_proof() -> None:
    assert "no_record" in check([*PAGE, BUBBLE], records=()).tokens


@pending("§8.8 no_record: a record only written, never dispatched")
def test_a_record_still_in_phase_written_is_no_record() -> None:
    written = dataclasses.replace(RECORD, phase="written")
    assert "no_record" in check([*PAGE, BUBBLE], records=(written,)).tokens


@pending("[V2.15] Q1: the record binds the proof by the subgoal id it holds")
def test_a_record_of_another_subgoal_is_no_record() -> None:
    other = dataclasses.replace(RECORD, subgoal="s2")
    assert "no_record" in check([*PAGE, BUBBLE], records=(other,)).tokens


@pending("§3.3 strict rule 3 param_not_sent")
def test_a_param_other_than_the_one_typed_is_param_not_sent() -> None:
    to_line = element(11, "ListItem", "the team", (5, 11), 4)
    result = check([*PAGE, to_line], cited=claim(to_line.id, param="to"))
    assert "param_not_sent" in result.tokens


@pending("§3.3 strict rule 4, T1 not_new ([V2.17].7)")
def test_content_already_in_the_records_before_is_not_new() -> None:
    old = dataclasses.replace(RECORD, before=snapshot([*PAGE, BUBBLE]))
    assert "not_new" in check([*PAGE, BUBBLE], records=(old,)).tokens


@pending("§3.3 strict rule 4 duplicate_effect")
def test_two_new_matching_elements_are_a_duplicate_effect() -> None:
    twin = element(11, "ListItem", "ok thanks", (5, 11), 4)
    assert "duplicate_effect" in check([*PAGE, BUBBLE, twin]).tokens


@pending("§3.3 strict tier: the earliest record's before is used")
def test_a_second_record_never_hides_a_duplicate() -> None:
    later = CommitRecord(
        action_key="k-send", subgoal="s1", phase="acting", before=snapshot([*PAGE, BUBBLE])
    )
    twin = element(11, "ListItem", "ok thanks", (5, 11), 4)
    assert "duplicate_effect" in check([*PAGE, BUBBLE, twin], records=(RECORD, later)).tokens


@pending("§3.3 strict rule 4, T1 no_match ([V2.17].7)")
def test_part_of_the_content_is_no_match() -> None:
    part = element(12, "ListItem", "ok", (5, 12), 4)
    assert "no_match" in check([*PAGE, part], cited=claim(part.id)).tokens


@pending("§3.3 inside_composer, a v2 token with no T1 equivalent")
def test_the_content_inside_the_composer_is_inside_composer() -> None:
    echo = element(13, "Text", "ok thanks", (5, 13), 2)
    assert "inside_composer" in check([*PAGE, echo], cited=claim(echo.id)).tokens


@pending("§3.3 composer_not_cleared, a v2 token with no T1 equivalent")
def test_a_field_still_filled_is_composer_not_cleared() -> None:
    assert "composer_not_cleared" in check([*PAGE, BUBBLE], filled=True).tokens


@pending("§3.3 strict rule 6 composer_unreadable")
def test_an_unread_field_is_composer_unreadable() -> None:
    assert "composer_unreadable" in check([*PAGE, BUBBLE], filled=None).tokens


@pending("§3.3 strict rule 6: a field that is gone needs no read")
def test_a_composer_that_is_gone_passes_without_a_read() -> None:
    gone = [DOC, SEND, CONVERSATION, BUBBLE]
    assert check(gone, filled=None).proof is not None


@pending("§3.3 strict rule 1: the scope rule holds at the strict tier")
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


@pending("§7.1, §8.8 standard_only: a standard proof never closes a commit subgoal")
def test_a_standard_proof_never_closes_a_commit_subgoal() -> None:
    assert closes("send", proof("standard")) == "standard_only"
    assert closes("submit", proof("standard")) == "standard_only"


@pending("§8.8: a strict proof closes a commit subgoal")
def test_a_strict_proof_closes_a_commit_subgoal() -> None:
    assert closes("send", proof("strict")) is None


@pending("§7.1: a standard proof closes a commit-none subgoal")
def test_a_standard_proof_closes_a_commit_none_subgoal() -> None:
    assert closes("none", proof("standard")) is None


@pending("[V2.17].7: the strict tier passes T1's Appeared reasons through unchanged")
@pytest.mark.parametrize("reason", T1_REASONS)
def test_every_t1_reason_passes_through_the_strict_tier(reason: str) -> None:
    assert strict_reason(reason) == reason


@pending("[V2.17].7: a reason T1 never gives is refused, never mapped")
def test_a_reason_outside_t1_is_refused() -> None:
    with pytest.raises(ValueError, match="reason"):
        strict_reason("preexisting")
