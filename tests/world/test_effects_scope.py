"""The effect scope and `frame_changed` ([V2.3], the lead's ruling 2026-11-14, from the step 2a
probe in real Chrome): own_state, revealed and content_changed are computed inside the
outermost Document that holds the acted element; a change outside it is the token
`frame_changed`, which blocks `none` and is never a class. An acted element with no Document
above it compares the whole window. A key press takes the scope of the last targeted element."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from steerai.world.effects import PRECEDENCE, Observed, observe
from tests.world.test_effects_conditions import Row, _snap

WINDOW: Row = (1, "Window", "Shop - Browser", (9, 1), None)
TABS: Row = (2, "Tab", "Tabs", (9, 2), 1)
TAB: Row = (3, "TabItem", "Shop", (9, 3), 2)
BACK: Row = (4, "Button", "Back", (9, 4), 1)
PAGE: Row = (10, "Document", "Shop", (9, 10), 1)
FIELD: Row = (11, "Edit", "Search", (9, 11), 10)
LIST: Row = (12, "List", "Products", (9, 12), 10)
ITEM: Row = (13, "ListItem", "Blue shoes", (9, 13), 12)
BROWSER = [WINDOW, TABS, TAB, BACK, PAGE, FIELD, LIST, ITEM]
URL = "https://shop.test/"


def _obs(
    rows: Sequence[Row], *, title: str = "Shop", url: str | None = URL, **kw: object
) -> Observed:
    snapshot = replace(_snap(rows, **kw), title=title)  # type: ignore[arg-type]  # test kwargs
    return Observed(snapshot=snapshot, url=url, windows=(16,))


def _renamed(rows: Sequence[Row], row: Row, name: str) -> list[Row]:
    return [(r[0], r[1], name, r[3], r[4]) if r == row else r for r in rows]


# ---- 1. the browser frame is outside the page --------------------------------------------------


def test_a_tab_name_changing_alone_is_frame_changed_never_content_or_none() -> None:
    after = _renamed(BROWSER, TAB, "Shop - Memory usage - 17.4 MB")
    report = observe(_obs(BROWSER), _obs(after), acted=FIELD[3])
    assert report.classes == frozenset()
    assert report.tokens == frozenset({"frame_changed"})


def test_a_push_state_is_in_place_and_the_back_button_is_the_frame() -> None:
    after = _obs(BROWSER, url=URL + "?q=red+shoes", caps={BACK[0]: ("invoke",)})
    before = _obs(BROWSER, caps={BACK[0]: ()})
    report = observe(before, after, acted=FIELD[3], text="red shoes")
    assert report.classes == frozenset({"url_changed_in_place"})
    assert "frame_changed" in report.tokens
    assert "url_param" in report.carriers


def test_results_inside_the_page_are_still_content_changed() -> None:
    result: Row = (14, "ListItem", "Red shoes", (9, 14), 12)
    report = observe(_obs(BROWSER), _obs([*BROWSER, result]), acted=FIELD[3], text="red shoes")
    assert "content_changed" in report.classes
    assert "results" in report.carriers
    assert "frame_changed" not in report.tokens


def test_a_frame_element_appearing_or_closing_is_frame_changed() -> None:
    bubble: Row = (5, "Pane", "Download complete", (9, 5), 1)
    appeared = observe(_obs(BROWSER), _obs([*BROWSER, bubble]), acted=FIELD[3])
    closed = observe(_obs([*BROWSER, bubble]), _obs(BROWSER), acted=FIELD[3])
    for report in (appeared, closed):
        assert report.classes == frozenset()
        assert report.tokens == frozenset({"frame_changed"})


def test_a_title_change_beside_its_tab_name_is_still_title_only() -> None:
    after = _renamed(BROWSER, TAB, "(3) Shop")
    report = observe(_obs(BROWSER), _obs(after, title="(3) Shop"), acted=FIELD[3])
    assert {"title_only", "frame_changed"} <= report.tokens
    assert report.classes == frozenset()


# ---- 2. no Document above the acted element: the whole window ----------------------------------

NOTEPAD: Row = (1, "Window", "notes - Editor", (9, 1), None)
MENUBAR: Row = (2, "MenuBar", "Application", (9, 2), 1)
FILE: Row = (3, "MenuItem", "File", (9, 3), 2)
EDITOR: Row = (4, "Document", "Text editor", (9, 4), 1)
EDITOR_ROWS = [NOTEPAD, MENUBAR, FILE, EDITOR]


def test_a_native_menu_click_compares_the_whole_window() -> None:
    menu: Row = (5, "Menu", "File", (9, 5), 3)
    entry: Row = (6, "MenuItem", "Save", (9, 6), 5)
    report = observe(
        _obs(EDITOR_ROWS, url=None), _obs([*EDITOR_ROWS, menu, entry], url=None), acted=FILE[3]
    )
    assert "revealed" in report.classes
    assert "frame_changed" not in report.tokens


def test_a_menu_opened_from_a_native_menu_bar_as_a_sibling_is_content_changed() -> None:
    popup: Row = (5, "Menu", "File", (9, 5), 1)
    report = observe(
        _obs(EDITOR_ROWS, url=None), _obs([*EDITOR_ROWS, popup], url=None), acted=FILE[3]
    )
    assert "content_changed" in report.classes
    assert "frame_changed" not in report.tokens


# ---- 3. a key press takes the scope of the last targeted element -------------------------------


def test_a_key_press_takes_the_last_targets_scope() -> None:
    after = _renamed(BROWSER, TAB, "Shop - Memory usage - 17.4 MB")
    scoped = observe(_obs(BROWSER), _obs(after), scope=FIELD[3])
    assert scoped.tokens == frozenset({"frame_changed"})
    unscoped = observe(_obs(BROWSER), _obs(after))
    assert "content_changed" in unscoped.classes, "no target ever: the whole window"


def test_the_acted_element_sets_the_scope_before_the_last_target() -> None:
    after = _renamed(BROWSER, TAB, "Shop - Memory usage - 17.4 MB")
    report = observe(_obs(BROWSER), _obs(after), acted=TAB[3], scope=FIELD[3])
    assert "frame_changed" not in report.tokens, "the tab itself was acted on: no Document"


# ---- 4. own_state and the precedence -----------------------------------------------------------


def test_an_expand_that_grows_its_own_box_is_still_own_state() -> None:
    folder: Row = (14, "TreeItem", "Reports", (9, 14), 10)
    child: Row = (15, "TreeItem", "January", (9, 15), 14)
    before = _obs([*BROWSER, folder], caps={14: ("collapsed",)})
    after = _obs(
        [*BROWSER, folder, child], caps={14: ("expanded",)}, boxes={14: (0, 140, 100, 190)}
    )
    report = observe(before, after, acted=folder[3])
    assert {"own_state", "revealed"} <= report.classes
    assert report.primary == "own_state", "the acted element's own change ranks first in logs"


def test_own_state_ranks_above_revealed() -> None:
    assert PRECEDENCE.index("own_state") < PRECEDENCE.index("revealed")


def test_an_inner_document_that_vanishes_compares_the_whole_window() -> None:
    """#263 review: an iframe's Document gone after is not found again, and it was not the top
    one, so the whole window is compared: the change is content, never `frame_changed`."""
    frame: Row = (14, "Document", "Widget", (9, 14), 10)
    button: Row = (15, "Button", "Close widget", (9, 15), 14)
    report = observe(_obs([*BROWSER, frame, button]), _obs(BROWSER), acted=button[3])
    assert "frame_changed" not in report.tokens
    assert "dismissed" in report.tokens
