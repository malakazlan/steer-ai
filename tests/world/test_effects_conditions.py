"""The owner's four conditions on the effect observer ([V2.2], 2026-11-13), written before the
observer code:

1. `none` means proven nothing changed. A re-render or a dropdown closing is a token
   (`rerendered`, `dismissed`), never `none`. A box move alone may still be `none`.
2. `unknown`, and any unread fact, is never `none`.
3. The report keeps the full class set; `primary` is one class for logs only.
4. A changed window list is never `none`: a click that opens a window is the token
   `windows_changed`.
"""

from __future__ import annotations

from collections.abc import Sequence

from steerai.world.effects import Observed, observe
from steerai.world.elements import Element
from steerai.world.model import Snapshot

Row = tuple[int, str, str, tuple[int, ...], int | None]
"""(id, role, name, runtime_id, parent_id)."""

PAGE: Row = (1, "Document", "Shop", (9, 1), None)
FIELD: Row = (2, "Edit", "Search", (9, 2), 1)
LIST: Row = (3, "List", "Products", (9, 3), 1)
ITEM: Row = (4, "ListItem", "Blue shoes", (9, 4), 3)
URL = "https://shop.test/"


def _snap(
    rows: Sequence[Row],
    *,
    stable: bool = True,
    boxes: dict[int, tuple[int, int, int, int]] | None = None,
    caps: dict[int, tuple[str, ...]] | None = None,
    caps_read: bool = True,
) -> Snapshot:
    elements = [
        Element(
            id=i,
            depth=0 if parent is None else 1,
            role=role,
            name=name,
            box=(boxes or {}).get(i, (0, 10 * i, 100, 10 * i + 8)),
            runtime_id=rid,
            parent_id=parent,
            caps=(caps or {}).get(i, ()),
        )
        for i, role, name, rid, parent in rows
    ]
    return Snapshot(
        id="s",
        hwnd=0x10,
        process="app.exe",
        title="Shop",
        taken_at=0.0,
        elements=elements,
        node_count=len(elements),
        stable=stable,
        caps_read=caps_read,
    )


def _read(
    snapshot: Snapshot, *, url: str | None = URL, windows: tuple[int, ...] | None = (16,)
) -> Observed:
    return Observed(snapshot=snapshot, url=url, windows=windows)


BASE = [PAGE, FIELD, LIST, ITEM]


# ---- 1. `none` is proven: a re-render or a dismissal is a token, never `none` -----------------


def test_nothing_changed_and_every_fact_read_is_none() -> None:
    report = observe(_read(_snap(BASE)), _read(_snap(BASE)), acted=FIELD[3])
    assert report.classes == frozenset({"none"})
    assert report.unread == frozenset()


def test_a_re_render_is_a_token_never_none() -> None:
    rerendered_item = (5, "ListItem", "Blue shoes", (9, 55), 3)  # same role, name, parent
    report = observe(
        _read(_snap(BASE)), _read(_snap([PAGE, FIELD, LIST, rerendered_item])), acted=FIELD[3]
    )
    assert "rerendered" in report.tokens
    assert "none" not in report.classes


def test_a_dropdown_closing_is_dismissed_never_none() -> None:
    suggestions = (6, "List", "Suggestions", (9, 6), 2)
    suggestion = (7, "ListItem", "blue shoes size 9", (9, 7), 6)
    report = observe(
        _read(_snap([*BASE, suggestions, suggestion])), _read(_snap(BASE)), acted=FIELD[3]
    )
    assert report.tokens == frozenset({"dismissed"})
    assert "none" not in report.classes
    assert "content_changed" not in report.classes, "a removal is not an effect (NE-01)"


def test_a_box_move_alone_may_still_be_none() -> None:
    moved = _snap(BASE, boxes={ITEM[0]: (0, 90, 100, 98)})
    report = observe(_read(_snap(BASE)), _read(moved), acted=FIELD[3])
    assert report.classes == frozenset({"none"})
    assert report.tokens == frozenset({"moved"})


# ---- 2. `unknown` and anything unread is never `none` -----------------------------------------


def test_an_unstable_after_is_unknown_alone() -> None:
    report = observe(_read(_snap(BASE)), _read(_snap(BASE, stable=False)), acted=FIELD[3])
    assert report.classes == frozenset({"unknown"})


def test_an_unread_url_window_list_or_caps_is_never_none() -> None:
    for before, after, missing in (
        (_read(_snap(BASE), url=None), _read(_snap(BASE)), "url"),
        (_read(_snap(BASE)), _read(_snap(BASE), windows=None), "windows"),
        (_read(_snap(BASE)), _read(_snap(BASE, caps_read=False)), "caps"),
    ):
        report = observe(before, after, acted=FIELD[3])
        assert missing in report.unread
        assert "none" not in report.classes, missing


# ---- 3. the full class set; `primary` for logs only -------------------------------------------


def test_the_report_keeps_every_class_and_names_one_primary() -> None:
    results = (8, "ListItem", "Red shoes", (9, 8), 3)  # content outside the field
    dropdown = (6, "List", "Suggestions", (9, 6), 2)  # revealed under the acted field
    report = observe(_read(_snap(BASE)), _read(_snap([*BASE, results, dropdown])), acted=FIELD[3])
    assert {"revealed", "content_changed"} <= report.classes
    assert report.primary == "revealed"


# ---- 4. a changed window list is never `none` -------------------------------------------------


def test_a_click_that_opens_a_window_is_never_none() -> None:
    report = observe(_read(_snap(BASE)), _read(_snap(BASE), windows=(16, 32)), acted=FIELD[3])
    assert "windows_changed" in report.tokens
    assert "none" not in report.classes
