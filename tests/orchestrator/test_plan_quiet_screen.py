"""The planner reads a quiet screen (dev yt2, 2026-11-12: a replan read YouTube's results skeleton).

Measured 2026-11-12 (Chrome 157, n=3, an Enter search on YouTube, a UIA observe every 100 ms):
the title changes within 0.23 s, the tree then holds a 91-node skeleton for 0.72-0.94 s, and the
results land 1.2-2.8 s after the Enter (588-603 nodes). Three equal node counts 0.25 s apart
return the skeleton; a planner reading it sees no video and plans the search again."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from steerai import run as run_mod
from steerai.run import PLAN_QUIET_S, warm_up
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import Row, build

POLL_S = 0.25


class Page:
    """A page whose node count follows a timeline of (from_s, count) on a fake clock."""

    def __init__(self, timeline: list[tuple[float, int]]) -> None:
        self.timeline = timeline
        self.t = 0.0
        self.observed: list[float] = []

    def observe(self) -> Snapshot:
        self.observed.append(self.t)
        count = next(c for start, c in reversed(self.timeline) if self.t >= start)
        rows: list[Row] = [(i, "Group", f"n{i}", (42, i), None) for i in range(count)]
        return build(rows, snapshot_id="s", taken_at=self.t)

    def sleep(self, seconds: float) -> None:
        self.t += seconds


SKELETON = [(0.0, 79), (0.25, 91), (1.25, 180), (1.5, 310), (2.5, 598)]
"""The measured shape: a 91-node skeleton for 1.0 s, the results from 2.5 s."""


def test_a_quiet_window_outlasts_the_skeleton_and_returns_the_results() -> None:
    page = Page(SKELETON)
    snapshot = warm_up(
        page.observe, cap_s=10.0, monotonic=lambda: page.t, sleep=page.sleep, quiet_s=PLAN_QUIET_S
    )
    assert snapshot.node_count == 598
    assert page.t >= 2.5 + PLAN_QUIET_S, "quiet for the whole window after the last change"
    assert page.t < 2.5 + PLAN_QUIET_S + 2 * POLL_S


def test_three_equal_counts_alone_return_the_skeleton() -> None:
    """The first-contact rule, unchanged for its callers: the case this fix exists for."""
    page = Page(SKELETON)
    snapshot = warm_up(page.observe, cap_s=10.0, monotonic=lambda: page.t, sleep=page.sleep)
    assert snapshot.node_count == 91


def test_a_page_that_never_goes_quiet_is_read_at_the_cap() -> None:
    page = Page([(i * 0.25, 100 + i) for i in range(100)])
    snapshot = warm_up(
        page.observe, cap_s=4.0, monotonic=lambda: page.t, sleep=page.sleep, quiet_s=PLAN_QUIET_S
    )
    assert 4.0 <= page.t < 4.0 + POLL_S
    assert snapshot.node_count == 100 + len(page.observed) - 1


def test_a_page_quiet_from_the_start_is_read_after_one_window() -> None:
    page = Page([(0.0, 50)])
    snapshot = warm_up(
        page.observe, cap_s=10.0, monotonic=lambda: page.t, sleep=page.sleep, quiet_s=PLAN_QUIET_S
    )
    assert snapshot.node_count == 50
    assert PLAN_QUIET_S <= page.t < PLAN_QUIET_S + POLL_S


def test_a_clock_that_never_moves_still_ends() -> None:
    page = Page([(0.0, 50)])
    snapshot = warm_up(
        page.observe,
        cap_s=10.0,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        quiet_s=PLAN_QUIET_S,
    )
    assert snapshot.node_count == 50


@pytest.mark.parametrize("quiet_s", [0.0, -1.0])
def test_a_quiet_window_must_be_positive(quiet_s: float) -> None:
    page = Page([(0.0, 50)])
    with pytest.raises(ValueError, match="quiet_s"):
        warm_up(
            page.observe, cap_s=1.0, monotonic=lambda: page.t, sleep=page.sleep, quiet_s=quiet_s
        )


def test_every_plan_screen_waits_for_the_quiet_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run._screen` (the first plan and every replan) reads through the quiet window."""
    seen: dict[str, Any] = {}

    def fake_warm_up(observe: Callable[[], Snapshot], **kwargs: Any) -> Snapshot:
        seen.update(kwargs)
        return observe()

    class FakeModel:
        def __init__(self, _client: Any) -> None:
            pass

        def observe(self, _hwnd: int, **_kw: Any) -> Snapshot:
            return build([(1, "Button", "Go", (42, 1), None)], snapshot_id="s", taken_at=0.0)

    monkeypatch.setattr(run_mod, "warm_up", fake_warm_up)
    monkeypatch.setattr("steerai.probes.uia_snapshot.UiaClient", object)
    monkeypatch.setattr("steerai.world.model.WorldModel", FakeModel)
    run_mod._screen(0x10)
    assert seen["quiet_s"] == PLAN_QUIET_S
    assert seen["cap_s"] == run_mod.DEFAULT_WARMUP_S
