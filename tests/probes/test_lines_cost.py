"""The lines-cost probe's "before" is exactly the pre-D-OS v2 listing: on-screen elements only."""

from __future__ import annotations

from steerai.probes.lines_cost import PAGES, before_lines
from steerai.world.elements import Element
from steerai.world.model import Snapshot


def _snapshot() -> Snapshot:
    root = Element(id=0, depth=0, role="Pane", name="Feed", box=(0, 0, 1000, 700))
    on = Element(id=1, depth=1, role="Button", name="Search", box=(10, 10, 90, 40), parent_id=0)
    below = Element(
        id=2,
        depth=1,
        role="Button",
        name="Load more",
        box=(10, 900, 90, 930),
        offscreen=True,
        parent_id=0,
    )
    return Snapshot(
        id="s",
        hwnd=1,
        process="chrome.exe",
        title="Feed",
        taken_at=0.0,
        elements=[root, on, below],
        node_count=3,
        stable=True,
    )


def test_before_lines_leave_out_offscreen_elements_that_lines_now_list() -> None:
    snapshot = _snapshot()
    assert not any("Load more" in line for line in before_lines(snapshot))
    assert any("Load more" in line for line in snapshot.lines)


def test_every_page_has_a_goal_and_a_start() -> None:
    assert len({page.name for page in PAGES}) == len(PAGES)
    assert all(page.goal and page.start for page in PAGES)
