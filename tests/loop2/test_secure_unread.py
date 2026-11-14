"""[V2.26] (2), issue #301: a field whose secure flag was not read is refused for typing (a
token and a strike, never an ask) and shows `secure_unread` in the v2 listing."""

from __future__ import annotations

import dataclasses

from steerai.loop2.controller import listing_rows
from steerai.loop2.loop import type_refusal
from tests.loop2.world_fakes import FIELD, GO, PAGE, PASSWORD, snapshot

UNREAD = dataclasses.replace(snapshot(PAGE), unread_secure=frozenset({FIELD.id}))


def test_a_type_into_a_field_with_an_unread_secure_flag_is_refused() -> None:
    assert type_refusal(FIELD, UNREAD) == "secure_unread"


def test_a_type_into_a_secure_field_is_refused_as_secure() -> None:
    assert type_refusal(PASSWORD, snapshot(PAGE)) == "secure_field"


def test_a_type_into_a_read_plain_field_is_not_refused() -> None:
    assert type_refusal(FIELD, snapshot(PAGE)) is None


def test_the_listing_shows_secure_unread_on_that_field_only() -> None:
    rows = {row.text.split(" ", 1)[0]: row.text for row in listing_rows(UNREAD)}
    assert rows[str(FIELD.id)].endswith(" secure_unread")
    assert "secure_unread" not in rows[str(GO.id)]


def test_the_listing_keeps_v1s_line_for_every_read_element() -> None:
    from steerai.world.elements import format_lines  # noqa: PLC0415 - v1's own formatter

    plain = snapshot(PAGE)
    assert [row.text for row in listing_rows(plain)] == format_lines(plain.elements)
