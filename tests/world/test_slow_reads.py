"""[V2.34] slow reads: no time rule passes without reads. A quiet window counts only when two
reads span it; a guard never expires without one read after the last change; every time is the
time a read COMPLETED. Each read here takes 2 s on a fake clock (wk3's cheap poll took 1.4 s),
against a 0.5 s quiet window. Reads are logged by tier, count and milliseconds."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from steerai.world.effects import Observed, poll_tier
from steerai.world.observe2 import first_observation, hold_still
from steerai.world.reads import ReadCost
from steerai.world.watch import SUBMIT_GUARD_S, Watched, watch
from tests.world.test_effects_conditions import BASE, FIELD, URL, Row, _snap
from tests.world.test_watch import Clock

READ_S = 2.0
POLL_S, SETTLE_S = 0.1, 0.5
ITEM2: Row = (5, "ListItem", "Red shoes", (9, 5), 3)
ITEM3: Row = (6, "ListItem", "Red boots", (9, 6), 3)
ITEM4: Row = (7, "ListItem", "Red hats", (9, 7), 3)
RESULTS: list[Row] = [
    (1, "Document", "Results for red shoes", (9, 100), None),
    (2, "List", "Hits", (9, 101), 1),
    (3, "ListItem", "Shoe one", (9, 102), 2),
]


def _full(rows: Sequence[Row], **facts: Any) -> Observed:  # Any: Observed fields, mixed types
    return dataclasses.replace(Observed(snapshot=_snap(rows), url=URL, windows=(16,)), **facts)


class SlowPage:
    """Every read takes `READ_S` and shows the screen as it was when the read STARTED (the
    worst case: a read that completes on a stale tree)."""

    def __init__(
        self,
        clock: Clock,
        frames: Sequence[tuple[float, Sequence[Row]]],
        facts: Sequence[tuple[float, dict[str, Any]]] = (),  # Any: Observed fields
    ) -> None:
        self.clock, self.frames, self.facts = clock, frames, facts
        self.polls: list[float] = []

    def _rows(self, at: float) -> Sequence[Row]:
        return [r for t, r in self.frames if t <= at][-1]

    def poll(self) -> Observed:
        started = self.clock.t
        self.clock.t += READ_S
        self.polls.append(started)
        return poll_tier(_snap(self._rows(started)))

    def full(self, seen: Observed) -> Observed:
        started = self.clock.t
        self.clock.t += READ_S
        extra: dict[str, Any] = {}  # Any: Observed fields
        for t, facts in self.facts:
            if t <= started:
                extra |= facts
        return dataclasses.replace(
            Observed(snapshot=seen.snapshot, url=URL, windows=(16,)), **extra
        )


def _watch(
    page: SlowPage,
    clock: Clock,
    *,
    full: bool = False,
    submit: bool = False,
    deadline_s: float = 10.0,
) -> Watched:
    return watch(
        page.poll,
        _full(BASE),
        acted=FIELD[3],
        clock=clock.now,
        sleep=clock.sleep,
        poll_s=POLL_S,
        settle_s=SETTLE_S,
        deadline_s=deadline_s,
        full=page.full if full or submit else None,
        submit=submit,
    )


# ---- the watch ---------------------------------------------------------------------------------


def test_a_quiet_window_needs_two_reads_to_span_it() -> None:
    """One 2 s read is longer than the 0.5 s window but cannot show the screen held still."""
    clock = Clock()
    result = _watch(SlowPage(clock, [(0.0, BASE)]), clock, full=True)
    assert result.report.classes == frozenset({"none"})
    assert result.timed_out is False
    assert result.read_cost == ReadCost(poll_reads=2, poll_ms=4000, full_reads=1, full_ms=2000)


def test_a_change_settles_only_on_a_later_read_and_from_its_completion() -> None:
    """The change arrives after the first read started; that read must not settle `none`."""
    clock = Clock()
    page = SlowPage(clock, [(0.0, BASE), (1.0, [*BASE, ITEM2])])
    result = _watch(page, clock)
    assert "content_changed" in result.report.classes
    assert result.settled_ms == 4100, "the change read's completion, not its start"
    assert result.read_cost.poll_reads == 3


def test_the_guard_never_expires_on_a_change_it_has_not_read_again() -> None:
    """The last read before the deadline shows a change: one more read before the guard ends,
    and the report is that read's."""
    clock = Clock()
    frames = [
        (0.0, BASE),
        (1.0, [*BASE, ITEM2]),
        (3.0, [*BASE, ITEM2, ITEM3]),
        (6.0, [*BASE, ITEM2, ITEM3, ITEM4]),
    ]
    result = _watch(SlowPage(clock, frames), clock, deadline_s=5.0)
    assert result.timed_out is True
    assert result.read_cost.poll_reads == 4
    assert any(e.name == "Red hats" for e in result.after.snapshot.elements)


def test_a_full_read_counts_from_its_completion() -> None:
    """The quiet-window full read that sees an in-place URL change completes 2 s after the
    poll it follows: the conclusive time is the full read's completion."""
    clock = Clock()
    page = SlowPage(clock, [(0.0, BASE)], [(0.0, {"url": f"{URL}?q=red"})])
    result = _watch(page, clock, submit=True, deadline_s=SUBMIT_GUARD_S)
    assert "url_changed_in_place" in result.report.classes
    assert result.conclusive_ms == 6100
    assert result.read_cost == ReadCost(poll_reads=2, poll_ms=4000, full_reads=1, full_ms=2000)


def test_the_submit_hold_holds_to_the_guard_on_slow_reads() -> None:
    clock = Clock()
    page = SlowPage(clock, [(0.0, BASE), (0.05, [*BASE, ITEM2])])
    result = _watch(page, clock, submit=True, deadline_s=SUBMIT_GUARD_S)
    assert result.report.classes == frozenset({"content_changed"})
    assert result.timed_out is True
    assert clock.t >= SUBMIT_GUARD_S


def test_the_submit_hold_sees_a_navigation_that_arrives_on_slow_reads() -> None:
    clock = Clock()
    page = SlowPage(clock, [(0.0, BASE), (0.05, [*BASE, ITEM2]), (3.0, RESULTS)])
    result = _watch(page, clock, submit=True, deadline_s=SUBMIT_GUARD_S)
    assert "navigated" in result.report.classes
    assert result.conclusive_ms is not None


# ---- the first observation and the hold ------------------------------------------------------

PID = 4242


def _page(nodes: int) -> list[Row]:
    rows: list[Row] = [(1, "Document", "Shop", (9, 1), None)]
    rows += [(i + 2, "ListItem", f"Item {i}", (9, 100 + i), 1) for i in range(nodes)]
    return rows


class SlowScreen:
    """Every full read takes `READ_S` and shows the page at its completion."""

    def __init__(self, clock: Clock, nodes: Any) -> None:  # Any: a callable of the time
        self.clock, self.nodes, self.reads = clock, nodes, 0

    def read(self) -> Observed:
        self.clock.t += READ_S
        self.reads += 1
        snapshot = dataclasses.replace(_snap(_page(self.nodes(self.clock.t))), pid=PID)
        return Observed(snapshot=snapshot)


def test_hold_still_never_ends_at_its_guard_on_a_change_it_has_not_read_again() -> None:
    clock = Clock()
    screen = SlowScreen(clock, lambda t: 3 + int(t * 2))
    first = Observed(snapshot=dataclasses.replace(_snap(_page(3)), pid=PID))
    got = hold_still(
        screen.read, first, quiet_s=SETTLE_S, guard_s=3.0, clock=clock.now, sleep=clock.sleep
    )
    assert screen.reads == 3, "the read after the guard's last change"
    assert got.growing is True
    assert got.read_cost is not None
    assert got.read_cost.full_reads == 3


def test_a_settled_page_needs_two_reads_on_slow_reads() -> None:
    clock = Clock()
    screen = SlowScreen(clock, lambda _t: 5)
    got = first_observation(screen.read, quiet_s=SETTLE_S, clock=clock.now, sleep=clock.sleep)
    assert got.snapshot.stable is True
    assert got.growing is False
    assert got.read_cost == ReadCost(poll_reads=0, poll_ms=0, full_reads=2, full_ms=4000)


def test_the_grow_guard_holds_on_slow_reads() -> None:
    clock = Clock()
    screen = SlowScreen(clock, lambda t: 3 + int(t * 2))
    got = first_observation(screen.read, quiet_s=SETTLE_S, clock=clock.now, sleep=clock.sleep)
    assert got.growing is True
    assert got.snapshot.stable is True
    assert got.read_cost is not None
    assert got.read_cost.full_reads == screen.reads
