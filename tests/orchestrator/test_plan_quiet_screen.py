"""The planner reads a quiet screen (dev yt2, 2026-11-12: a replan read YouTube's results skeleton).

Measured 2026-11-12 (Chrome 157, n=3, an Enter search on YouTube, a UIA observe every 100 ms):
the title changes within 0.23 s, the tree then holds a 91-node skeleton for 0.72-0.94 s, and the
results land 1.2-2.8 s after the Enter (588-603 nodes). Three equal node counts 0.25 s apart
return the skeleton; a planner reading it sees no video and plans the search again."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from steerai import run as run_mod
from steerai.errors import ErrorCode, SteerError
from steerai.run import PLAN_QUIET_S, WarmRead, warm_up, warm_up_read
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import Row, build

POLL_S = 0.25


class Page:
    """A page whose node count follows a timeline of (from_s, count) on a fake clock; between
    `shaky` seconds a walk is unstable, between `failing` seconds the provider answers
    `perception_timeout`."""

    def __init__(
        self,
        timeline: list[tuple[float, int]],
        *,
        shaky: tuple[float, float] = (-1.0, -1.0),
        failing: tuple[float, float] = (-1.0, -1.0),
    ) -> None:
        self.timeline = timeline
        self.shaky = shaky
        self.failing = failing
        self.t = 0.0
        self.observed: list[float] = []

    def observe(self) -> Snapshot:
        self.observed.append(self.t)
        if self.failing[0] <= self.t < self.failing[1]:
            raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "not yet")
        count = next(c for start, c in reversed(self.timeline) if self.t >= start)
        rows: list[Row] = [(i, "Group", f"n{i}", (42, i), None) for i in range(count)]
        snapshot = build(rows, snapshot_id="s", taken_at=self.t)
        stable = not self.shaky[0] <= self.t < self.shaky[1]
        return snapshot if stable else replace(snapshot, stable=False)

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


def test_an_unstable_walk_at_an_equal_count_restarts_the_quiet_window() -> None:
    """Principal item 1 on #232: a tree changing during the walk is not quiet (as `_quiet`)."""
    page = Page([(0.0, 50)], shaky=(0.0, 1.5))
    warm_up(
        page.observe, cap_s=10.0, monotonic=lambda: page.t, sleep=page.sleep, quiet_s=PLAN_QUIET_S
    )
    assert page.t >= 1.25 + PLAN_QUIET_S, "quiet only from the last shaky or failed look (1.25 s)"


def test_a_provider_that_is_not_ready_restarts_the_quiet_window() -> None:
    page = Page([(0.0, 50)], failing=(0.5, 1.5))
    snapshot = warm_up(
        page.observe, cap_s=10.0, monotonic=lambda: page.t, sleep=page.sleep, quiet_s=PLAN_QUIET_S
    )
    assert snapshot.node_count == 50
    assert page.t >= 1.25 + PLAN_QUIET_S, "quiet only from the last shaky or failed look (1.25 s)"


@pytest.mark.parametrize("poll_s", [0.0, -0.25])
def test_a_poll_interval_must_be_positive(poll_s: float) -> None:
    page = Page([(0.0, 50)])
    with pytest.raises(ValueError, match="poll_s"):
        warm_up(page.observe, cap_s=1.0, poll_s=poll_s, quiet_s=PLAN_QUIET_S)


@pytest.mark.parametrize("quiet_s", [0.0, -1.0])
def test_a_quiet_window_must_be_positive(quiet_s: float) -> None:
    page = Page([(0.0, 50)])
    with pytest.raises(ValueError, match="quiet_s"):
        warm_up(
            page.observe, cap_s=1.0, monotonic=lambda: page.t, sleep=page.sleep, quiet_s=quiet_s
        )


def test_a_quiet_read_is_not_capped_and_a_read_that_never_settles_is() -> None:
    """#233: the plan read's capped rate is traced, so the read says how it ended."""
    quiet = Page(SKELETON)
    read = warm_up_read(
        quiet.observe, cap_s=10.0, monotonic=lambda: quiet.t, sleep=quiet.sleep, quiet_s=2.0
    )
    assert (read.snapshot.node_count, read.capped) == (598, False)
    busy = Page([(i * 0.25, 100 + i) for i in range(100)])
    read = warm_up_read(
        busy.observe, cap_s=4.0, monotonic=lambda: busy.t, sleep=busy.sleep, quiet_s=2.0
    )
    assert read.capped


def test_a_kill_during_the_quiet_read_stops_it_before_the_next_observe() -> None:
    """TK-22 on the plan read (#233): the read lasts 2-10 s, the kill is checked every poll."""
    page = Page(SKELETON)
    killed = {"now": False}

    def sleep(seconds: float) -> None:
        page.sleep(seconds)
        killed["now"] = page.t >= 0.5

    with pytest.raises(SteerError) as info:
        warm_up_read(
            page.observe,
            cap_s=10.0,
            monotonic=lambda: page.t,
            sleep=sleep,
            quiet_s=PLAN_QUIET_S,
            kill=lambda: killed["now"],
        )
    assert info.value.code is ErrorCode.GATE_BLOCKED
    assert page.observed == [0.0, 0.25]


def test_every_plan_screen_reads_quiet_under_the_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run._screen` (the first plan and every replan) reads through the quiet window, passes
    the kill switch on, and says whether the read hit the cap."""
    seen: dict[str, Any] = {}

    def fake_read(observe: Callable[[], Snapshot], **kwargs: Any) -> WarmRead:
        seen.update(kwargs)
        return WarmRead(observe(), capped=True)

    class FakeModel:
        def __init__(self, _client: Any) -> None:
            pass

        def observe(self, _hwnd: int, **_kw: Any) -> Snapshot:
            return build([(1, "Button", "Go", (42, 1), None)], snapshot_id="s", taken_at=0.0)

    def kill() -> bool:
        return False

    monkeypatch.setattr(run_mod, "warm_up_read", fake_read)
    monkeypatch.setattr("steerai.probes.uia_snapshot.UiaClient", object)
    monkeypatch.setattr("steerai.world.model.WorldModel", FakeModel)
    read = run_mod._screen(0x10, kill)
    assert seen["quiet_s"] == PLAN_QUIET_S
    assert seen["cap_s"] == run_mod.DEFAULT_WARMUP_S
    assert seen["kill"] is kill
    assert read.capped
    assert len(read.lines) == 1
