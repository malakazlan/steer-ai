"""[V2.27], [V2.28] 2: the first-observation probe's rows and the live check, without a browser."""

from __future__ import annotations

import pytest

from steerai.probes.first_observation import FIELDS, check_row, pooled_site, snapshot_row
from steerai.probes.live_checks import CHECKS
from tests.world.test_effects_conditions import BASE, PAGE, _snap

EDIT = (5, "Edit", "Email", (9, 5), 1)
NOTES = (6, "Edit", "Notes", (9, 6), 1)
OMNIBOX = (7, "Edit", "Address and search bar", (9, 7), None)
"""Chrome's address bar: an Edit outside the page's Document."""


def test_a_first_read_with_an_empty_document_counts_no_children_and_no_fields() -> None:
    row = snapshot_row(0, _snap([PAGE]))
    assert (row["document"], row["document_children"], row["fields"]) == (True, 0, 0)
    assert row["document_nodes"] == 0


def test_a_built_page_counts_its_children_nodes_and_fields() -> None:
    row = snapshot_row(250, _snap(BASE))
    assert row["document_children"] == 2
    assert row["document_nodes"] == 3
    assert row["fields"] == 1
    assert row["node_count"] == len(BASE)


def test_the_omnibox_is_not_a_page_field() -> None:
    """[V2.28] 2: only fields inside the Document count (run 2 passed on the omnibox alone)."""
    assert snapshot_row(0, _snap([PAGE, OMNIBOX]))["fields"] == 0
    assert check_row(_snap([*BASE, EDIT, OMNIBOX]))["fields"] == 2


def test_the_check_passes_when_every_field_is_seen() -> None:
    row = check_row(_snap([*BASE, EDIT, NOTES]), expected=len(FIELDS))
    assert "error" not in row
    assert row["fields"] == 3


def test_the_check_fails_loudly_when_fields_are_missing() -> None:
    assert check_row(_snap([PAGE]))["error"] == "fields_missing"


def test_two_page_fields_and_the_omnibox_fail_the_check() -> None:
    assert check_row(_snap([*BASE, EDIT, OMNIBOX]))["error"] == "fields_missing"


def test_the_check_fails_on_an_unstable_observation() -> None:
    assert check_row(_snap([*BASE, EDIT, NOTES], stable=False))["error"] == "fields_missing"


def test_the_check_prints_stable_document_children_and_node_count() -> None:
    row = check_row(_snap([*BASE, EDIT, NOTES]))
    assert row["stable"] is True
    assert row["document_children"] == 4
    assert row["document_nodes"] == 5


def test_the_check_says_whether_the_page_was_still_growing() -> None:
    """[V2.31] a growing page may be handed over; the row says so, it does not fail on it."""
    row = check_row(_snap([*BASE, EDIT, NOTES]), growing=True)
    assert row["growing"] is True
    assert "error" not in row
    assert check_row(_snap([*BASE, EDIT, NOTES]))["growing"] is False


def test_a_pooled_site_is_its_task_start() -> None:
    assert pooled_site("gh1") == "https://github.com/"


@pytest.mark.parametrize("task_id", ["h7s2", "hn1", "g1", "nope"])
def test_a_sealed_desktop_dev_set_or_unknown_task_is_refused(task_id: str) -> None:
    """Never H7 (sealed); never a desktop task; the dev set is not pooled."""
    with pytest.raises(ValueError, match="not_a_pooled_site"):
        pooled_site(task_id)


def test_the_live_check_is_registered() -> None:
    check = next(c for c in CHECKS if c.name == "first_observation")
    assert check.args == ("steerai.probes.first_observation", "--check")
