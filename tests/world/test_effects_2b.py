"""V2 step 2b, the observer's new facts ([V2.5], the lead's conditions; probe #266, Chrome 157,
n=1): `own_value` from the acted element's value (kept only as `filled` and a keyed digest),
`focus` as a class, `dialog_opened`, and the heading carrier. A fact that was not read is not
observed: it never yields a class and never becomes `unread`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

import pytest

from steerai.world.effects import PRECEDENCE, Observed, ValueFact, observe, value_fact
from tests.world.test_effects_conditions import BASE, FIELD, LIST, PAGE, Row, _snap

URL = "https://shop.test/"


def _obs(
    rows: Sequence[Row],
    *,
    focus: tuple[int, ...] | None = None,
    value: ValueFact | None = None,
    headings: Mapping[tuple[int, ...], int] | None = None,
    aria: Mapping[tuple[int, ...], str] | None = None,
    url: str | None = URL,
) -> Observed:
    return Observed(
        snapshot=_snap(rows),
        url=url,
        windows=(16,),
        focus=focus,
        acted_value=value,
        headings=headings,
        aria_roles=aria,
    )


KEY = b"k" * 32


# ---- the value stays private ------------------------------------------------------------------


def test_a_value_fact_keeps_only_filled_and_a_keyed_digest() -> None:
    fact = value_fact("red shoes", key=KEY)
    assert fact.filled is True
    assert "red" not in repr(fact)
    assert fact == value_fact("red shoes", key=KEY)
    assert fact != value_fact("red shoes", key=b"x" * 32), "keyed: another key, another digest"
    assert value_fact("", key=KEY).filled is False


def test_a_short_key_is_refused() -> None:
    with pytest.raises(ValueError, match="key"):
        value_fact("x", key=b"short")


# ---- own_value -----------------------------------------------------------------------------


def test_a_changed_value_on_the_acted_field_is_own_value() -> None:
    before = _obs(BASE, value=value_fact("", key=KEY))
    after = _obs(BASE, value=value_fact("red shoes", key=KEY))
    report = observe(before, after, acted=FIELD[3])
    assert report.classes == frozenset({"own_value"})


def test_an_unread_value_is_not_observed_never_unread_and_never_own_value() -> None:
    report = observe(_obs(BASE), _obs(BASE, value=value_fact("x", key=KEY)), acted=FIELD[3])
    assert "own_value" not in report.classes
    assert report.unread == frozenset()
    assert report.classes == frozenset({"none"}), "type's read-back verifies typing ([V2.5])"


def test_the_same_value_is_no_own_value() -> None:
    fact = value_fact("red shoes", key=KEY)
    report = observe(_obs(BASE, value=fact), _obs(BASE, value=fact), acted=FIELD[3])
    assert report.classes == frozenset({"none"})


# ---- focus ---------------------------------------------------------------------------------


def test_a_focus_move_is_the_class_focus_never_none() -> None:
    report = observe(_obs(BASE, focus=(9, 1)), _obs(BASE, focus=FIELD[3]), acted=FIELD[3])
    assert report.classes == frozenset({"focus"})


def test_focus_read_on_one_side_only_is_not_observed() -> None:
    report = observe(_obs(BASE), _obs(BASE, focus=FIELD[3]), acted=FIELD[3])
    assert "focus" not in report.classes
    assert report.classes == frozenset({"none"})


# ---- dialog_opened -------------------------------------------------------------------------


def test_a_new_window_control_inside_the_scope_is_dialog_opened_and_revealed() -> None:
    dialog: Row = (6, "Window", "Rename project", (9, 6), 1)
    report = observe(_obs(BASE), _obs([*BASE, dialog]), acted=FIELD[3])
    assert {"dialog_opened", "revealed"} <= report.classes
    assert "none" not in report.classes


def test_an_aria_dialog_role_on_a_new_element_is_dialog_opened() -> None:
    sheet: Row = (6, "Group", "Confirm", (9, 6), 1)
    after = _obs([*BASE, sheet], aria={(9, 6): "alertdialog"})
    report = observe(_obs(BASE), after, acted=FIELD[3])
    assert "dialog_opened" in report.classes


def test_a_dialog_outside_the_scope_is_dialog_opened_with_frame_changed() -> None:
    window: Row = (20, "Window", "Shop - Browser", (8, 1), None)
    page: Row = (1, "Document", "Shop", (9, 1), 20)
    rows = [window, page, *BASE[1:]]
    prompt: Row = (30, "Window", "Allow notifications?", (8, 30), 20)
    report = observe(_obs(rows), _obs([*rows, prompt]), acted=FIELD[3])
    assert "dialog_opened" in report.classes
    assert "frame_changed" in report.tokens
    assert "revealed" not in report.classes


# ---- the heading carrier -------------------------------------------------------------------


def test_a_new_top_heading_carrying_the_text_is_the_heading_carrier() -> None:
    title: Row = (7, "Text", "Red shoes", (9, 7), 1)
    after = _obs([*BASE, title], headings={(9, 7): 1})
    report = observe(_obs(BASE, headings={}), after, acted=FIELD[3], text="red shoes")
    assert "heading" in report.carriers


def test_an_unchanged_heading_is_no_carrier() -> None:
    title: Row = (7, "Text", "Red shoes", (9, 7), 1)
    rows = [*BASE, title]
    facts: dict[tuple[int, ...], int] = {(9, 7): 1}
    report = observe(
        _obs(rows, headings=facts), _obs(rows, headings=facts), acted=FIELD[3], text="red shoes"
    )
    assert "heading" not in report.carriers


def test_only_the_top_level_heading_counts() -> None:
    top: Row = (7, "Text", "Shop", (9, 7), 1)
    sub: Row = (8, "Text", "Red shoes", (9, 8), 3)
    after = _obs([*BASE, top, sub], headings={(9, 7): 1, (9, 8): 2})
    report = observe(_obs(BASE, headings={}), after, acted=FIELD[3], text="red shoes")
    assert "heading" not in report.carriers


def test_headings_not_read_give_no_heading_carrier() -> None:
    title: Row = (7, "Text", "Red shoes", (9, 7), 1)
    report = observe(_obs(BASE), _obs([*BASE, title]), acted=FIELD[3], text="red shoes")
    assert "heading" not in report.carriers


# ---- precedence (logs only) ----------------------------------------------------------------


def test_the_new_classes_have_a_place_in_the_log_precedence() -> None:
    for name in ("own_value", "focus", "dialog_opened"):
        assert name in PRECEDENCE
    assert PRECEDENCE.index("own_value") == PRECEDENCE.index("own_state") + 1
    assert PRECEDENCE.index("dialog_opened") < PRECEDENCE.index("own_state")
    assert replace(_obs(BASE), focus=None).focus is None
    assert LIST[0] != PAGE[0]


# ---- [V2.24] 1: where the dialog opened, as an explicit fact ----------------------------------

CHROME_WINDOW: Row = (20, "Window", "Shop - Browser", (8, 1), None)
IN_WINDOW_PAGE: Row = (1, "Document", "Shop", (9, 1), 20)
FRAMED = [CHROME_WINDOW, IN_WINDOW_PAGE, *BASE[1:]]
PROMPT: Row = (30, "Window", "Allow notifications?", (8, 30), 20)


def test_no_dialog_has_no_dialog_scope() -> None:
    assert observe(_obs(BASE), _obs(BASE), acted=FIELD[3]).dialog_scope is None


def test_a_dialog_under_the_acted_element_is_inside() -> None:
    dialog: Row = (6, "Window", "Rename project", (9, 6), 1)
    report = observe(_obs(BASE), _obs([*BASE, dialog]), acted=FIELD[3])
    assert report.dialog_scope == "inside"


def test_an_in_page_modal_elsewhere_in_the_document_is_inside() -> None:
    """[V2.24]: `revealed` covers only what appears under the acted element; a modal elsewhere
    in the page is still inside the scope Document."""
    modal: Row = (6, "Group", "Sign in", (9, 6), 1)
    after = _obs([*BASE, modal], aria={(9, 6): "dialog"})
    report = observe(_obs(BASE), after, acted=FIELD[3])
    assert "dialog_opened" in report.classes
    assert report.dialog_scope == "inside"


def test_a_dialog_outside_the_scope_document_is_outside() -> None:
    report = observe(_obs(FRAMED), _obs([*FRAMED, PROMPT]), acted=FIELD[3])
    assert report.dialog_scope == "outside"


def test_any_dialog_outside_makes_the_fact_outside() -> None:
    inside: Row = (6, "Group", "Sign in", (9, 6), 1)
    after = _obs([*FRAMED, inside, PROMPT], aria={(9, 6): "dialog"})
    report = observe(_obs(FRAMED), after, acted=FIELD[3])
    assert report.dialog_scope == "outside"


def test_with_no_scope_document_the_whole_window_is_inside() -> None:
    """[V2.11] item 7: with no scope Document the whole window is the scope."""
    root: Row = (1, "Window", "Notes", (7, 1), None)
    save: Row = (2, "Button", "Save", (7, 2), 1)
    dialog: Row = (3, "Window", "Save as", (7, 3), 1)
    report = observe(_obs([root, save]), _obs([root, save, dialog]), acted=save[3])
    assert report.dialog_scope == "inside"
