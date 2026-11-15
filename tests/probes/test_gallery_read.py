"""The gallery read probe's pure part: the read is taken only once Chrome exposes the last rows."""

from __future__ import annotations

from steerai.probes.gallery_read import ready
from tests.loop2.world_fakes import element, snapshot

DOC = element(1, "Document", "Steer gallery", (3, 1), None)
FIND = element(2, "Edit", "Find articles", (3, 2), 1)
TERMS = element(3, "Hyperlink", " Terms of  service ", (3, 3), 1)


def test_the_read_waits_for_the_last_rows() -> None:
    assert ready(snapshot([DOC, FIND, TERMS])) is True
    assert ready(snapshot([DOC, FIND])) is False
    assert ready(snapshot([DOC])) is False


def test_a_row_of_another_role_is_not_the_row() -> None:
    other = element(4, "Text", "Terms of service", (3, 4), 1)
    assert ready(snapshot([DOC, FIND, other])) is False
