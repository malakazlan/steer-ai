"""Offscreen lines (offscreen-lines packet D-OS v2, principal REVISE items 2 and 3, hunter OS-23/
29/30/32/42/53): the planner sees elements scrolled out of view, flagged, after every on-screen
line; only those a scroll can reach (horizontal overlap with the window, measured 5/5 by
`offscreen_reach`), none from a minimized window; chosen nearest first, printed in tree order."""

from __future__ import annotations

from dataclasses import replace

from steerai.world.elements import Box, Element, format_lines, prune
from steerai.world.model import Snapshot

WINDOW: Box = (0, 0, 1000, 700)


def _el(
    eid: int,
    role: str,
    name: str,
    box: Box,
    *,
    parent: int | None = 0,
    offscreen: bool = False,
    depth: int = 1,
) -> Element:
    return Element(
        id=eid,
        depth=depth,
        role=role,
        name=name,
        box=box,
        offscreen=offscreen,
        runtime_id=(42, eid),
        parent_id=parent,
    )


ROOT = _el(0, "Pane", "Page", WINDOW, parent=None, depth=0)
ON = _el(1, "Button", "Search", (10, 10, 100, 40))
FAR = _el(2, "Button", "Far below", (10, 5000, 100, 5030), offscreen=True)
NEAR = _el(3, "Button", "Load more", (10, 900, 100, 930), offscreen=True)
SR_ONLY = _el(4, "Text", "Skip to content", (-12440, 900, -12290, 920), offscreen=True)
ABOVE = _el(5, "Hyperlink", "Back to top", (10, -400, 100, -380), offscreen=True)


def _ids(elements: list[Element]) -> list[int]:
    return [e.id for e in elements]


def test_offscreen_elements_are_listed_after_every_on_screen_line() -> None:
    kept = prune([ROOT, FAR, ON, NEAR], cap=150, offscreen=True)
    assert _ids(kept) == [0, 1, 2, 3]


def test_without_the_option_prune_is_unchanged() -> None:
    assert _ids(prune([ROOT, FAR, ON, NEAR], cap=150)) == [0, 1]


def test_an_element_a_scroll_cannot_reach_is_never_listed() -> None:
    """sr-only text at x=-12440 stays offscreen after ScrollIntoView 5/5 (probe)."""
    assert 4 not in _ids(prune([ROOT, ON, SR_ONLY], cap=150, offscreen=True))


def test_a_minimized_window_lists_nothing_offscreen() -> None:
    kept = prune([ROOT, ON, NEAR], cap=150, offscreen=True, minimized=True)
    assert _ids(kept) == [0, 1]


def test_the_cap_cuts_the_farthest_first_and_never_an_on_screen_line() -> None:
    kept = prune([ROOT, ON, FAR, NEAR, ABOVE], cap=4, offscreen=True)
    assert _ids(kept) == [0, 1, 3, 5], "the nearest two of three, printed in tree order"
    assert _ids(prune([ROOT, ON, FAR, NEAR], cap=2, offscreen=True)) == [0, 1]


def test_each_offscreen_group_keeps_its_ancestor_line() -> None:
    feed = _el(6, "List", "Feed", (0, 800, 1000, 1200), offscreen=True)
    item = _el(7, "ListItem", "Post 9", (10, 1100, 900, 1150), parent=6, offscreen=True, depth=2)
    kept = prune([ROOT, ON, feed, item], cap=150, offscreen=True)
    assert _ids(kept) == [0, 1, 6, 7]


def test_an_ancestor_that_does_not_fit_drops_its_child_too() -> None:
    feed = _el(6, "Group", "", (0, 800, 1000, 1200), offscreen=True)
    item = _el(7, "Button", "Load more", (10, 900, 100, 930), parent=6, offscreen=True, depth=2)
    assert _ids(prune([ROOT, ON, feed, item], cap=3, offscreen=True)) == [0, 1]


def test_zero_area_offscreen_elements_stay_out() -> None:
    flat = _el(8, "Button", "Nothing", (10, 900, 10, 900), offscreen=True)
    assert 8 not in _ids(prune([ROOT, ON, flat], cap=150, offscreen=True))


def test_the_line_carries_the_flag_outside_the_quoted_name() -> None:
    forged = _el(9, "Button", 'Pay" offscreen', (10, 10, 100, 40))
    lines = format_lines([NEAR, forged])
    assert lines[0].endswith(" offscreen")
    assert not lines[1].endswith(" offscreen"), "a name can never forge the flag (OS-42)"


def _snapshot(*elements: Element, minimized: bool = False) -> Snapshot:
    return Snapshot(
        id="s",
        hwnd=1,
        process="chrome.exe",
        title="Feed",
        taken_at=0.0,
        elements=list(elements),
        node_count=len(elements),
        stable=True,
        minimized=minimized,
    )


def test_snapshot_lines_list_offscreen_elements_and_count_what_the_cap_cut() -> None:
    snapshot = _snapshot(ROOT, ON, FAR, NEAR)
    lines = snapshot.lines
    assert lines[-1].endswith(" offscreen")
    assert any('"Load more"' in line for line in lines)
    capped = replace(snapshot, cap=3)
    assert capped.lines[-1] == "(... 1 more not shown)"


def test_a_page_with_nothing_offscreen_keeps_byte_identical_lines() -> None:
    """OS-53: Notepad has no offscreen elements (probe), so its lines must not change."""
    snapshot = _snapshot(ROOT, ON)
    assert snapshot.lines == format_lines(prune([ROOT, ON], cap=150))


def test_snapshot_lines_of_a_minimized_window_list_nothing_offscreen() -> None:
    assert not any(" offscreen" in line for line in _snapshot(ROOT, ON, NEAR, minimized=True).lines)
