"""[V2.28] 1, the confirmed-observation rule in the watch: a submit action (anything through the
Enter table) ends early only on a conclusive report; anything else holds to the navigation
guard, with the quiet-window full reads. The Enter table is evaluated once, by the caller, on
the final report, so rows 4, 8 and 9 come only after the hold. Plain clicks do not hold."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

import pytest

from steerai.loop2.query import STRONG, query_outcome
from steerai.world.effects import Observed, ValueFact, observe, poll_tier
from steerai.world.watch import CARRIED, SUBMIT_GUARD_S, Watched, conclusive, watch
from tests.loop2.world_fakes import report
from tests.world.test_effects_conditions import BASE, FIELD, URL, Row, _snap
from tests.world.test_watch import Clock

ITEM2: Row = (5, "ListItem", "Red shoes", (9, 5), 3)
DIALOG: Row = (7, "Window", "Leave site?", (9, 7), 1)
RESULTS: list[Row] = [
    (1, "Document", "Results for red shoes", (9, 100), None),
    (2, "List", "Hits", (9, 101), 1),
    (3, "ListItem", "Shoe one", (9, 102), 2),
]
"""Another page: a new top Document with other names (a navigation, not a re-render)."""
POLL_S, SETTLE_S = 0.1, 0.5
FILLED = ValueFact(filled=True, digest="a")
EMPTY = ValueFact(filled=False, digest="b")


def _full(rows: Sequence[Row], **facts: Any) -> Observed:  # Any: Observed fields, mixed types
    return dataclasses.replace(Observed(snapshot=_snap(rows), url=URL, windows=(16,)), **facts)


class Page:
    """A page over time: each frame is the screen from its time on; `facts` are what the full
    tier adds to it from their own times on."""

    def __init__(
        self,
        clock: Clock,
        frames: Sequence[tuple[float, Sequence[Row]]],
        facts: Sequence[tuple[float, dict[str, Any]]] = (),  # Any: Observed fields
    ) -> None:
        self.clock, self.frames, self.facts = clock, frames, facts
        self.full_at: list[float] = []

    def poll(self) -> Observed:
        rows = [r for t, r in self.frames if t <= self.clock.t][-1]
        return poll_tier(_snap(rows))

    def full(self, seen: Observed) -> Observed:
        self.full_at.append(self.clock.t)
        extra: dict[str, Any] = {}  # Any: Observed fields
        for t, facts in self.facts:
            if t <= self.clock.t:
                extra |= facts
        read = Observed(snapshot=seen.snapshot, url=URL, windows=(16,))
        return dataclasses.replace(read, **extra)


def _watch(
    page: Page,
    before: Observed,
    clock: Clock,
    *,
    submit: bool = True,
    trigger: bool = False,
    text: str | None = None,
) -> Watched:
    return watch(
        page.poll,
        before,
        acted=FIELD[3],
        text=text,
        clock=clock.now,
        sleep=clock.sleep,
        poll_s=POLL_S,
        settle_s=SETTLE_S,
        deadline_s=SUBMIT_GUARD_S,
        full=page.full,
        trigger=trigger,
        submit=submit,
    )


def test_the_guard_is_five_seconds() -> None:
    assert SUBMIT_GUARD_S == 5.0


def test_the_carried_classes_are_the_enter_tables_strong_classes_without_a_url_move() -> None:
    assert CARRIED | {"navigated", "url_changed_in_place"} == STRONG


# ---- what holds: content with no carrier, weak, nothing --------------------------------------


def test_content_with_no_carrier_holds_to_the_guard_and_is_row_4() -> None:
    """Live run 2: 13 of 24 samples ended on this shape at 47-109 ms, then navigated."""
    clock = Clock()
    page = Page(clock, [(0.0, BASE), (0.1, [*BASE, ITEM2])])
    result = _watch(page, _full(BASE), clock)
    assert result.report.classes == frozenset({"content_changed"})
    assert clock.t >= SUBMIT_GUARD_S
    assert result.timed_out is True
    assert result.conclusive_ms is None
    assert result.weak_reads >= 2, "the quiet-window full reads go on during the hold"
    assert query_outcome(result.report, text_carried=False).row == "4"


def test_the_same_page_as_a_plain_trigger_ends_early() -> None:
    """Plain clicks do not hold: the [V2.8] strong report still ends the watch."""
    clock = Clock()
    page = Page(clock, [(0.0, BASE), (0.1, [*BASE, ITEM2])])
    result = _watch(page, _full(BASE), clock, submit=False, trigger=True)
    assert result.report.classes == frozenset({"content_changed"})
    assert clock.t < 1.0
    assert result.conclusive_ms is None


def test_a_weak_change_holds_to_the_guard_and_is_row_8() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE)], [(0.05, {"focus": (9, 3)})])
    result = _watch(page, _full(BASE, focus=(9, 2)), clock)
    assert result.report.classes == frozenset({"focus"})
    assert clock.t >= SUBMIT_GUARD_S
    assert query_outcome(result.report, text_carried=False).row == "8"


def test_nothing_holds_to_the_guard_and_is_row_9() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE)])
    result = _watch(page, _full(BASE), clock)
    assert result.report.classes == frozenset({"none"})
    assert clock.t >= SUBMIT_GUARD_S
    assert query_outcome(result.report, text_carried=False).row == "9"


@pytest.mark.parametrize(
    ("frames", "facts", "row"),
    [
        ([(0.0, BASE), (0.1, [*BASE, ITEM2])], [], "4"),
        ([(0.0, BASE)], [(0.05, {"focus": (9, 3)})], "8"),
        ([(0.0, BASE)], [], "9"),
    ],
    ids=["row4", "row8", "row9"],
)
def test_rows_4_8_and_9_are_only_produced_after_the_hold(
    frames: list[tuple[float, Sequence[Row]]],
    facts: list[tuple[float, dict[str, Any]]],
    row: str,
) -> None:
    """The caller evaluates the table once, on the watch's final report: these rows can only
    come from a report read at the guard."""
    clock = Clock()
    page = Page(clock, frames, facts)
    result = _watch(page, _full(BASE, focus=(9, 2)), clock)
    assert query_outcome(result.report, text_carried=False).row == row
    assert page.full_at[-1] >= SUBMIT_GUARD_S
    assert clock.t >= SUBMIT_GUARD_S


# ---- what ends early ---------------------------------------------------------------------------


def test_a_late_navigation_ends_the_hold_and_records_its_arrival() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE), (0.1, [*BASE, ITEM2]), (3.0, RESULTS)])
    result = _watch(page, _full(BASE), clock)
    assert "navigated" in result.report.classes
    assert result.timed_out is False
    assert result.conclusive_ms is not None
    assert 3000 <= result.conclusive_ms <= 3100, "seen by the first poll after it arrived"
    assert clock.t < SUBMIT_GUARD_S
    assert query_outcome(result.report, text_carried=True).row == "6"


def test_an_in_place_url_change_ends_at_the_next_full_read() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE), (0.1, [*BASE, ITEM2])], [(1.0, {"url": f"{URL}?q=red"})])
    result = _watch(page, _full(BASE), clock)
    assert "url_changed_in_place" in result.report.classes
    assert result.timed_out is False
    assert result.conclusive_ms is not None
    assert result.conclusive_ms >= 1000
    assert clock.t < SUBMIT_GUARD_S


def test_content_with_a_carrier_ends_early() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE), (0.1, [*BASE, ITEM2])])
    result = _watch(page, _full(BASE), clock, text="red shoes")
    assert "content_changed" in result.report.classes
    assert "results" in result.report.carriers
    assert clock.t < 1.0
    assert result.conclusive_ms is not None


def test_a_dialog_ends_early() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE), (0.1, [*BASE, DIALOG])])
    result = _watch(page, _full(BASE), clock)
    assert "dialog_opened" in result.report.classes
    assert clock.t < 1.0


def test_a_window_change_ends_early() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE)], [(0.05, {"windows": (16, 17)})])
    result = _watch(page, _full(BASE), clock)
    assert "windows_changed" in result.report.tokens
    assert clock.t < 1.0
    assert query_outcome(result.report, text_carried=False).row == "1"


def test_the_typed_field_emptied_ends_early() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE)], [(0.05, {"acted_value": EMPTY})])
    result = _watch(page, _full(BASE, acted_value=FILLED), clock)
    assert clock.t < 1.0
    assert result.after.acted_value == EMPTY


def test_a_field_that_stays_filled_holds() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE)], [(0.05, {"acted_value": FILLED})])
    result = _watch(page, _full(BASE, acted_value=FILLED), clock)
    assert clock.t >= SUBMIT_GUARD_S
    assert result.conclusive_ms is None


# ---- the predicate and the contract ------------------------------------------------------------


def test_conclusive_reads_only_the_ruled_facts() -> None:
    before, after = _full(BASE), _full([*BASE, ITEM2])
    report = observe(before, after, acted=FIELD[3])
    assert conclusive(report, before, after) is False
    carried = observe(before, after, acted=FIELD[3], text="red shoes")
    assert conclusive(carried, before, after) is True


def test_a_submit_watch_needs_the_full_read() -> None:
    clock = Clock()
    with pytest.raises(ValueError, match="full"):
        watch(
            lambda: poll_tier(_snap(BASE)),
            _full(BASE),
            clock=clock.now,
            sleep=clock.sleep,
            poll_s=POLL_S,
            settle_s=SETTLE_S,
            deadline_s=SUBMIT_GUARD_S,
            submit=True,
        )


def test_a_watch_that_is_not_a_submit_has_no_conclusive_time() -> None:
    clock = Clock()
    page = Page(clock, [(0.0, BASE), (0.1, RESULTS)])
    result = _watch(page, _full(BASE), clock, submit=False, trigger=True)
    assert "navigated" in result.report.classes
    assert result.conclusive_ms is None


def test_a_field_that_goes_from_empty_to_filled_is_not_conclusive() -> None:
    before = _full(BASE, acted_value=EMPTY)
    after = _full(BASE, acted_value=FILLED)
    assert conclusive(report("none"), before, after) is False
