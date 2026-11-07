"""A text step whose field shows another text role resolves by its exact name (Phase 2 chooser
wiring, schema gate REVISE: the deterministic alias route only; live run osg-4 named `Edit` where
Notepad shows `Document`). Only `focus` and `type`, only R0/R1, only Edit/Document/ComboBox,
exactly one match; the model never changes the element."""

from __future__ import annotations

from dataclasses import replace

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_loop import Harness, _type


def _as_document(h: Harness, row_id: int = COMPOSER[0]) -> None:
    h.world.rows = [(r[0], "Document", *r[2:]) if r[0] == row_id else r for r in h.world.rows]


def test_a_type_into_a_field_shown_under_another_text_role_resolves_by_name() -> None:
    h = Harness()
    _as_document(h)
    result = h.session.run_step(_type())
    assert result.ok is True, result.to_dict()
    assert result.evidence["alias"] == "Document"


def test_a_focus_resolves_the_same_way() -> None:
    h = Harness()
    _as_document(h)
    focus = Step(id="f1", subgoal="g1", verb="focus", target=Target(role="Edit", name="Message"))
    assert h.session.run_step(focus).evidence.get("alias") == "Document"


def test_a_click_never_takes_an_alias() -> None:
    h = Harness()
    _as_document(h)
    click = Step(
        id="c1", subgoal="g1", verb="click", target=Target(role="Edit", name="Message"), risk="R0"
    )
    result = h.session.run_step(click)
    assert (result.code, result.evidence["detail"]) == (ErrorCode.ELEMENT_NOT_FOUND, "no_match")


def test_an_r2_step_never_takes_an_alias() -> None:
    h = Harness()
    _as_document(h)
    result = h.session.run_step(replace(_type(), risk="R2"))
    assert result.evidence["detail"] == "no_match"
    assert "alias" not in result.evidence or result.evidence["alias"] == "none"


def test_two_text_fields_with_the_name_are_ambiguous() -> None:
    h = Harness()
    _as_document(h)
    twin = (99, "ComboBox", "Message", (42, 99), 7)
    h.world.rows = [*h.world.rows, twin]
    result = h.session.run_step(_type())
    assert result.evidence["detail"] == "ambiguous_target"


def test_no_text_field_with_the_name_stays_no_match() -> None:
    h = Harness()
    h.world.rows = [r for r in h.world.rows if r[0] != COMPOSER[0]]
    result = h.session.run_step(_type())
    assert result.evidence["detail"] == "no_match"
    assert result.evidence["alias"] == "none"


def test_a_resolved_element_whose_own_words_raise_the_tier_stops() -> None:
    """Gate item 3: the tier is read on the element that would be touched (its automation id
    names a delete), never only on the plan's words."""
    h = Harness()
    _as_document(h)
    snapshot = h.world.observe(0x10)
    elements = [
        replace(e, automation_id="deleteAccountField") if e.id == COMPOSER[0] else e
        for e in snapshot.elements
    ]
    marked = replace(snapshot, elements=elements)
    with pytest.raises(Exception, match="alias_risk"):
        h.session._resolve_alias(_type(), marked, {})


def test_a_resume_wait_keeps_the_alias() -> None:
    """Gate item 4: after an ask or a wait the step re-resolves the same element, not no_match."""
    h = Harness()
    _as_document(h)
    snapshot = h.world.observe(0x10)
    evidence: dict[str, object] = {}
    first = h.session._resolve_alias(_type(), snapshot, evidence)
    again, element_id = h.session._reobserve(_type(), snapshot, evidence)
    assert element_id == first
    assert again.element(element_id).role == "Document"


def test_every_later_resolve_in_the_step_keeps_the_alias() -> None:
    """Hunter CH-12: the act retry and the scroll re-resolve as the step first resolved."""
    h = Harness()
    _as_document(h)
    snapshot = h.world.observe(0x10)
    first = h.session._resolve_alias(_type(), snapshot, {})
    assert h.session._resolve_again(_type(), snapshot) == first


def test_a_secure_field_is_never_an_alias() -> None:
    """Hunter CH-04: a password field is never typed into by a role alias."""
    h = Harness()
    _as_document(h)
    snapshot = h.world.observe(0x10)
    elements = [replace(e, secure=True) if e.id == COMPOSER[0] else e for e in snapshot.elements]
    with pytest.raises(Exception, match="no_match"):
        h.session._resolve_alias(_type(), replace(snapshot, elements=elements), {})
