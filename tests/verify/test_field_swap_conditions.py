"""The condition side of the field swap (packet wiki-field-swap-2026-11-11, R2 bullet 3; hunter
FS-03, FS-12, FS-20, FS-21): a click's reveal `appeared Edit "X"` holds when the page shows a
same-named ComboBox instead (Wikipedia, 1 of 12 live runs: the reveal went straight to the
ComboBox), counted on both roles in both snapshots so a role swap of a field that was there is
never an appearance; plain form only, never Document, never `replaces`, one expected."""

from __future__ import annotations

import pytest

from steerai.verify.predicates import Appeared, Disappeared, Present, Within
from steerai.verify.t1 import evaluate
from steerai.world.model import Snapshot
from tests.verify.fakes import Row, build

PAGE: list[Row] = [
    (0, "Pane", "Wikipedia - Google Chrome", (42, 1), None),
    (1, "Document", "Wikipedia", (42, 2), 0),
    (2, "Hyperlink", "Search", (42, 3), 1),
]
EDIT: Row = (3, "Edit", "Search Wikipedia", (42, 10), 1)
COMBO: Row = (3, "ComboBox", "Search Wikipedia", (42, 11), 1)
COMBO_TWIN: Row = (4, "ComboBox", "Search Wikipedia", (42, 12), 1)
DOC: Row = (3, "Document", "Search Wikipedia", (42, 13), 1)

REVEAL = Appeared(role="Edit", name_pattern="Search Wikipedia")


def _before(*rows: Row) -> Snapshot:
    return build([*PAGE, *rows], snapshot_id="before")


def _after(*rows: Row) -> Snapshot:
    return build([*PAGE, *rows], snapshot_id="after", taken_at=1.0)


def test_a_reveal_that_went_straight_to_the_combobox_holds_with_the_alias() -> None:
    verdict = evaluate(REVEAL, _before(), _after(COMBO))
    assert verdict.holds is True, verdict
    assert verdict.evidence.get("alias") == "ComboBox"


def test_the_exact_role_wins_and_carries_no_alias() -> None:
    verdict = evaluate(REVEAL, _before(), _after(EDIT))
    assert verdict.holds is True
    assert "alias" not in verdict.evidence


def test_a_field_that_was_there_under_the_other_role_is_not_new() -> None:
    """FS-03: the Edit was on the page; the click only swapped its role."""
    verdict = evaluate(REVEAL, _before(EDIT), _after(COMBO))
    assert verdict.holds is False
    assert verdict.evidence.get("reason") == "not_new"


def test_a_combobox_present_in_both_snapshots_is_not_new() -> None:
    """FS-21: a warm widget."""
    verdict = evaluate(REVEAL, _before(COMBO), _after(COMBO))
    assert verdict.holds is False
    assert verdict.evidence.get("reason") == "not_new"


def test_two_same_named_comboboxes_never_satisfy_one_expected_edit() -> None:
    verdict = evaluate(REVEAL, _before(), _after(COMBO, COMBO_TWIN))
    assert verdict.holds is False


def test_a_document_never_aliases_and_a_replacing_or_counted_form_never_does() -> None:
    """FS-12: the pairs are explicit and the navigation form keeps its exact role."""
    assert evaluate(REVEAL, _before(), _after(DOC)).holds is False
    document = Appeared(role="Document", name_pattern="Search Wikipedia")
    assert evaluate(document, _before(), _after(COMBO)).holds is False
    with pytest.raises(ValueError, match="replaces"):
        Appeared(role="Edit", name_pattern="Search Wikipedia", replaces=True)
    counted = Appeared(role="Edit", name_pattern="Search Wikipedia", expected=2)
    assert evaluate(counted, _before(), _after(COMBO, COMBO_TWIN)).holds is False


def test_a_present_of_the_field_role_reads_the_swapped_combobox() -> None:
    present = Present(
        role="Edit", name_pattern="Search Wikipedia", within=Within(landmark="Wikipedia")
    )
    verdict = evaluate(present, _before(COMBO), _after(COMBO))
    assert verdict.holds is True, verdict
    assert verdict.evidence.get("alias") == "ComboBox"


def test_a_disappeared_field_shown_under_the_other_role_has_not_gone() -> None:
    """FS-20: the search box did not close; it swapped."""
    gone = Disappeared(role="Edit", name_pattern="Search Wikipedia")
    verdict = evaluate(gone, _before(EDIT), _after(COMBO))
    assert verdict.holds is False
    assert verdict.evidence.get("reason") == "swapped"


def test_aswapped_field_is_found_after_the_suggestion_rows_shifted_every_id() -> None:
    """Principal re-review item 1: Wikipedia's +17 suggestion rows move the positional ids;
    the swapped check resolves its scope on `after`, never with `before`'s ids."""
    rows: list[Row] = [(3 + i, "ListItem", f"suggestion {i}", (42, 100 + i), 1) for i in range(17)]
    shifted: Row = (20, "ComboBox", "Search Wikipedia", (42, 11), 1)
    gone = Disappeared(role="Edit", name_pattern="Search Wikipedia")
    verdict = evaluate(gone, _before(EDIT), _after(*rows, shifted))
    assert verdict.holds is False
    assert verdict.evidence.get("reason") == "swapped"


def test_a_field_without_a_runtime_id_is_never_the_swapped_one() -> None:
    """Principal re-review item 2: an element that cannot be proven new is not taken."""
    from steerai.verify.t1 import swapped_field  # noqa: PLC0415 - the rule's own unit

    before = _before(EDIT)
    bare: Row = (3, "ComboBox", "Search Wikipedia", (), 1)
    element, evidence = swapped_field(before.element(3), before, _after(bare))
    assert element is None
    assert evidence == {}
    found, evidence = swapped_field(before.element(3), before, _after(COMBO))
    assert found is not None
    assert found.role == "ComboBox"
    assert evidence == {"alias": "ComboBox"}
