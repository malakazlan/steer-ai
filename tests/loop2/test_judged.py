"""[V2.67] A, D: the auto proof is one judgement that carries every condition it rests on, so a
subgoal that did not close says which condition failed. Its verdict is `auto_proof`'s."""

from __future__ import annotations

import dataclasses

from steerai.loop2.verify import Judged, auto_proof, judge
from tests.loop2.test_navigated import QUERY_URL, SUB, TYPED, before, page
from tests.loop2.world_fakes import FIELD, RESULT, URL


def _held(judged: Judged, carrier: str) -> tuple[bool, bool | None]:
    ((_name, facts),) = judged.params
    return next((held, was) for c, held, was in facts if c == carrier)


def test_a_proven_read_names_its_change_carriers_and_content() -> None:
    judged = judge(SUB, before(), page([RESULT]), typed=TYPED)
    assert judged.proof == "results_with_url"
    assert judged.refused == ()
    assert judged.change_by == ("url_changed_in_place",)
    assert judged.content_by == "results_with_url"
    assert _held(judged, "url_param") == (True, False)
    assert _held(judged, "results") == (True, False)


def test_the_verdict_is_the_auto_proofs() -> None:
    for now in (page([RESULT]), page([RESULT], url=URL), page([], url=QUERY_URL)):
        assert judge(SUB, before(), now, typed=TYPED).proof == auto_proof(
            SUB, before(), now, typed=TYPED
        )


def test_an_unread_address_is_named() -> None:
    unread = dataclasses.replace(page([RESULT]), document_url=None)
    judged = judge(SUB, before(), unread, typed=TYPED)
    assert (judged.proof, judged.refused) == (None, ("url_unread",))


def test_a_carrier_with_no_content_names_the_missing_content() -> None:
    judged = judge(SUB, before(), page([], url=QUERY_URL), typed=TYPED)
    assert judged.proof is None
    assert judged.content_by is None
    assert _held(judged, "url_param") == (True, False)


def test_a_result_row_rejected_by_a_guard_is_named() -> None:
    """The typed field itself holds the words: the result rule names `inside_field`."""
    typed_field = dataclasses.replace(FIELD, name="red shoes")
    now = page([], url=QUERY_URL)
    shown = [typed_field if e.id == FIELD.id else e for e in now.snapshot.elements]
    elements = type(now.snapshot.elements)(shown)
    now = dataclasses.replace(now, snapshot=dataclasses.replace(now.snapshot, elements=elements))
    judged = judge(SUB, before(), now, typed=TYPED)
    assert judged.proof is None
    assert "inside_field" in judged.guards
