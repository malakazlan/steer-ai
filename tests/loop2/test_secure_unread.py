"""[V2.26] (2), issue #301: a field whose secure flag was not read is refused for typing (a
token and a strike, never an ask) and shows `secure_unread` in the v2 listing."""

from __future__ import annotations

import dataclasses

from steerai.loop2.controller import v2_listing
from steerai.loop2.decision import Decision
from steerai.loop2.loop import type_refusal
from steerai.loop2.runner import run_task
from steerai.world.model import Snapshot
from tests.loop2.world_fakes import FIELD, GO, PAGE, PASSWORD, World, observed, snapshot, task

UNREAD = dataclasses.replace(snapshot(PAGE), unread_secure=frozenset({FIELD.id}))


def test_a_type_into_a_field_with_an_unread_secure_flag_is_refused() -> None:
    assert type_refusal(FIELD, UNREAD) == "secure_unread"


def test_a_type_into_a_secure_field_is_refused_as_secure() -> None:
    assert type_refusal(PASSWORD, snapshot(PAGE)) == "secure_field"


def test_a_type_into_a_read_plain_field_is_not_refused() -> None:
    assert type_refusal(FIELD, snapshot(PAGE)) is None


def _ordinals(shown: Snapshot) -> dict[int, int]:
    """Handles equal to the snapshot ordinals, so v1's line is unchanged."""
    return {e.id: e.id for e in shown.elements}


def test_the_listing_shows_secure_unread_on_that_field_only() -> None:
    rows = {
        row.text.split(" ", 1)[0]: row.text for row in v2_listing(UNREAD, handles=_ordinals(UNREAD))
    }
    assert rows[str(FIELD.id)].endswith(" secure_unread")
    assert "secure_unread" not in rows[str(GO.id)]


def test_the_listing_keeps_v1s_line_for_every_read_element() -> None:
    from steerai.world.elements import format_lines  # noqa: PLC0415 - v1's own formatter

    plain = snapshot(PAGE)
    assert [row.text for row in v2_listing(plain, handles=_ordinals(plain))] == format_lines(
        plain.elements
    )


def test_the_outcome_counts_every_unread_automation_id_it_took_in() -> None:
    """[V2.26] (2), V2 §6.1, #301: an unread automation id is recorded and its frequency counted
    on the pooled set; the decision on it stays open. Every observation the loop takes in
    (each one passes the stamp port) adds its snapshot's unread count."""
    seen = observed()
    blind = dataclasses.replace(
        seen, snapshot=dataclasses.replace(seen.snapshot, unread_automation_id=frozenset({2, 3}))
    )
    world = World([Decision(stop="cannot")], screen=blind)
    outcome = run_task(task(), world.ports())
    assert world.events.count("stamp") >= 2
    assert outcome.unread_automation_ids == 2 * world.events.count("stamp")


def test_a_read_automation_id_counts_nothing() -> None:
    world = World([Decision(stop="cannot")])
    assert run_task(task(), world.ports()).unread_automation_ids == 0
