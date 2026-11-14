"""The evidence rule ([V2.3]): cited evidence must be inside the scope Document; a tab name or
the address bar is never evidence. Red on the 3c stub."""

from __future__ import annotations

from steerai.loop2.decision import Claim
from steerai.loop2.verify import evidence_tokens
from tests.loop2.world_fakes import BAR, DOC, PAGE, RESULT, TAB, element, pending, snapshot

SCREEN = snapshot([*PAGE, RESULT])


def cite(*ids: int) -> Claim:
    return Claim(subgoal="s1", kind="navigated", evidence=list(ids))


@pending("[V2.3] and [V2.11].7 evidence inside the scope Document")
def test_evidence_inside_the_scope_document_is_admissible() -> None:
    assert evidence_tokens(cite(RESULT.id), SCREEN, scope_document=DOC.runtime_id) == ()


@pending("[V2.3] and [V2.11].7 evidence inside the scope Document")
def test_the_address_bar_is_never_evidence() -> None:
    tokens = evidence_tokens(cite(BAR.id), SCREEN, scope_document=DOC.runtime_id)
    assert "outside_scope" in tokens


@pending("[V2.3] and [V2.11].7 evidence inside the scope Document")
def test_a_tab_name_is_never_evidence() -> None:
    tokens = evidence_tokens(cite(TAB.id), SCREEN, scope_document=DOC.runtime_id)
    assert "outside_scope" in tokens


@pending("[V2.3] and [V2.11].7 evidence inside the scope Document")
def test_one_outside_id_rejects_the_whole_citation() -> None:
    tokens = evidence_tokens(cite(RESULT.id, BAR.id), SCREEN, scope_document=DOC.runtime_id)
    assert "outside_scope" in tokens


@pending("[V2.3] and [V2.11].7 evidence inside the scope Document")
def test_an_id_not_in_the_snapshot_is_not_listed() -> None:
    tokens = evidence_tokens(cite(99), SCREEN, scope_document=DOC.runtime_id)
    assert "id_not_listed" in tokens


@pending("[V2.3] and [V2.11].7 evidence inside the scope Document")
def test_without_a_document_the_window_is_the_scope() -> None:
    label = element(1, "Text", "Saved", (7, 1), None)
    assert evidence_tokens(cite(label.id), snapshot([label]), scope_document=None) == ()


@pending("[V2.3] and [V2.11].7 evidence inside the scope Document")
def test_evidence_in_another_document_is_outside_the_scope() -> None:
    other = element(20, "Document", "Other", (9, 20), None)
    inner = element(21, "Text", "Red shoes", (9, 21), 20)
    screen = snapshot([*PAGE, other, inner])
    tokens = evidence_tokens(cite(inner.id), screen, scope_document=DOC.runtime_id)
    assert "outside_scope" in tokens


@pending("[V2.3] and [V2.11].7 evidence inside the scope Document")
def test_with_no_scope_document_the_whole_window_is_allowed() -> None:
    assert evidence_tokens(cite(RESULT.id, TAB.id), SCREEN, scope_document=None) == ()
