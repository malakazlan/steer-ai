"""[V2.27] the first-observation probe's rows and the live check, without a browser."""

from __future__ import annotations

from steerai.probes.first_observation import FIELDS, check_row, snapshot_row
from steerai.probes.live_checks import CHECKS
from tests.world.test_effects_conditions import BASE, PAGE, _snap

EDIT = (5, "Edit", "Email", (9, 5), 1)
NOTES = (6, "Edit", "Notes", (9, 6), 1)


def test_a_first_read_with_an_empty_document_counts_no_children_and_no_fields() -> None:
    row = snapshot_row(0, _snap([PAGE]))
    assert (row["document"], row["document_children"], row["fields"]) == (True, 0, 0)


def test_a_built_page_counts_its_children_and_fields() -> None:
    row = snapshot_row(250, _snap(BASE))
    assert row["document_children"] == 2
    assert row["fields"] == 1


def test_the_check_passes_when_every_field_is_seen() -> None:
    row = check_row(_snap([*BASE, EDIT, NOTES]), expected=len(FIELDS))
    assert "error" not in row
    assert row["fields"] == 3


def test_the_check_fails_loudly_when_fields_are_missing() -> None:
    assert check_row(_snap([PAGE]))["error"] == "fields_missing"


def test_the_check_fails_on_an_unstable_observation() -> None:
    assert check_row(_snap([*BASE, EDIT, NOTES], stable=False))["error"] == "fields_missing"


def test_the_live_check_is_registered() -> None:
    check = next(c for c in CHECKS if c.name == "first_observation")
    assert check.args == ("steerai.probes.first_observation", "--check")
