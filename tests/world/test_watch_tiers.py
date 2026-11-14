"""[V2.10] the watch's two read tiers. Every read carries its tier and the observer refuses to
compare two reads of different tiers; each poll is compared with `before` seen through the poll
tier; unread alone is never a change. [V2.9] the quiet-window full read: final for a non-trigger
verb whatever it shows, final for a trigger only when strong, at most one per quiet window."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from steerai.world.effects import Observed, ValueFact, observe, poll_tier
from steerai.world.watch import Watched, watch
from tests.world.test_effects_conditions import BASE, FIELD, PAGE, URL, Row, _snap
from tests.world.test_watch import Clock

ITEM2: Row = (5, "ListItem", "Red shoes", (9, 5), 3)
TAB: Row = (10, "TabItem", "Shop", (9, 10), None)
"""A browser-frame element outside the page's Document."""
POLL_S, SETTLE_S, DEADLINE_S = 0.1, 0.3, 2.0


def _full(rows: Sequence[Row], **facts: Any) -> Observed:  # Any: Observed fields, mixed types
    return dataclasses.replace(Observed(snapshot=_snap(rows), url=URL, windows=(16,)), **facts)


def _polls(frames: list[Observed], at: list[float], clock: Clock) -> Callable[[], Observed]:
    def read() -> Observed:
        return frames[max(i for i, t in enumerate(at) if t <= clock.t)]

    return read


class FullReads:
    """The full tier: adds `facts` to whatever the poll saw, and records each call's time."""

    def __init__(self, clock: Clock, **facts: Any) -> None:  # Any: Observed fields, mixed types
        self.clock, self.facts, self.at = clock, facts, list[float]()

    def __call__(self, seen: Observed) -> Observed:
        self.at.append(self.clock.t)
        read = Observed(snapshot=seen.snapshot, url=URL, windows=(16,))
        return dataclasses.replace(read, **self.facts)


def _run(
    frames: list[Observed],
    at: list[float],
    full: FullReads,
    clock: Clock,
    *,
    trigger: bool,
    before: Observed | None = None,
) -> Watched:
    return watch(
        _polls(frames, at, clock),
        before if before is not None else _full(BASE),
        acted=FIELD[3],
        clock=clock.now,
        sleep=clock.sleep,
        poll_s=POLL_S,
        settle_s=SETTLE_S,
        deadline_s=DEADLINE_S,
        full=full,
        trigger=trigger,
    )


# ---- tiers -----------------------------------------------------------------------------------


def test_the_observer_refuses_two_reads_of_different_tiers() -> None:
    with pytest.raises(ValueError, match="tier_mismatch"):
        observe(_full(BASE), poll_tier(_snap(BASE)), acted=FIELD[3])


def test_two_poll_reads_of_the_same_screen_are_none_with_no_url_unread() -> None:
    report = observe(poll_tier(_snap(BASE)), poll_tier(_snap(BASE)), acted=FIELD[3])
    assert "url" not in report.unread, "the poll tier never reads the URL"
    assert "none" not in report.classes, "the window list is unread: never none"


# ---- the no-op regression ([V2.10] decision 1) ------------------------------------------------


def test_a_pure_no_op_has_no_first_change_and_a_non_trigger_ends_none_at_the_quiet_window() -> None:
    clock = Clock()
    full = FullReads(clock)
    result = _run([poll_tier(_snap(BASE))], [0.0], full, clock, trigger=False)
    assert result.first_change_ms is None
    assert result.report.classes == frozenset({"none"})
    assert result.timed_out is False
    assert full.at == [pytest.approx(SETTLE_S)], "one full read, at the quiet window, final"


def test_a_pure_no_op_after_a_trigger_ends_none_at_the_deadline() -> None:
    clock = Clock()
    full = FullReads(clock)
    result = _run([poll_tier(_snap(BASE))], [0.0], full, clock, trigger=True)
    assert result.first_change_ms is None
    assert result.report.classes == frozenset({"none"})
    assert result.timed_out is True
    gaps = [b - a for a, b in zip(full.at, full.at[1:], strict=False)]
    assert all(g >= SETTLE_S - 1e-9 for g in gaps[:-1]), "at most one full read per quiet window"
    assert len(full.at) <= int(DEADLINE_S / SETTLE_S) + 1
    assert result.weak_reads == len(full.at) - 1, "every quiet-window read but the final one"


def test_a_non_trigger_with_a_full_tier_only_effect_ends_at_the_quiet_window() -> None:
    """Typing into a field with nothing else changing: only the full read sees `own_value`."""
    clock = Clock()
    before = _full(BASE, acted_value=ValueFact(filled=False, digest="0"))
    full = FullReads(clock, acted_value=ValueFact(filled=True, digest="1"))
    result = _run([poll_tier(_snap(BASE))], [0.0], full, clock, trigger=False, before=before)
    assert "own_value" in result.report.classes
    assert result.seen_by == "full"
    assert full.at == [pytest.approx(SETTLE_S)]


# ---- the phase-1 quiet timer ([V2.9]) ---------------------------------------------------------


def test_frame_only_noise_does_not_restart_the_phase_1_quiet_timer() -> None:
    clock = Clock()
    noisy_tab = (10, "TabItem", "Shop - Memory usage", (9, 10), None)
    frames = [poll_tier(_snap([*BASE, TAB])), poll_tier(_snap([*BASE, noisy_tab]))]
    full = FullReads(clock, url=URL + "?q=red")
    result = _run(frames, [0.0, 0.2], full, clock, trigger=True, before=_full([*BASE, TAB]))
    assert "url_changed_in_place" in result.report.classes
    assert full.at[0] == pytest.approx(SETTLE_S), "the tab-name noise did not push the read out"
    assert len(full.at) == 1, "a strong quiet-window read is also the final read"


def test_a_change_inside_the_scope_restarts_the_phase_1_quiet_timer() -> None:
    clock = Clock()
    pressed = _snap(BASE, caps={FIELD[0]: ("toggle:on",)})
    frames = [poll_tier(_snap(BASE)), poll_tier(pressed)]
    full = FullReads(clock, url=URL + "?q=red")
    _run(frames, [0.0, 0.2], full, clock, trigger=True)
    assert full.at[0] == pytest.approx(0.2 + SETTLE_S)


# ---- which tier sees each strong class ([V2.9]) -----------------------------------------------

NEW_PAGE: Row = (1, "Document", "Results", (9, 100), None)
DIALOG: Row = (6, "Window", "Sign in", (9, 6), 1)
SUGGESTIONS: Row = (6, "List", "Suggestions", (9, 6), 2)
SUGGESTION: Row = (7, "ListItem", "blue shoes size 9", (9, 7), 6)
RERENDERED_ITEM: Row = (4, "ListItem", "Blue shoes", (9, 44), 3)


def _titled(rows: Sequence[Row], title: str) -> Observed:
    return poll_tier(dataclasses.replace(_snap(rows), title=title))


POLL_SEEN: list[tuple[str, list[Row], Observed, str]] = [
    ("content_changed", BASE, poll_tier(_snap([*BASE, ITEM2])), "class"),
    ("revealed", BASE, poll_tier(_snap([*BASE, SUGGESTIONS])), "class"),
    ("dialog_opened", BASE, poll_tier(_snap([*BASE, DIALOG])), "class"),
    ("navigated", BASE, poll_tier(_snap([NEW_PAGE, (2, "Link", "Next", (9, 101), 1)])), "class"),
    (
        "rerendered",
        BASE,
        poll_tier(_snap([PAGE, FIELD, (3, "List", "Products", (9, 3), 1), RERENDERED_ITEM])),
        "token",
    ),
    ("dismissed", [*BASE, SUGGESTIONS, SUGGESTION], poll_tier(_snap(BASE)), "token"),
    ("title_only", BASE, _titled(BASE, "Shop - page 2"), "token"),
]


@pytest.mark.parametrize(
    ("name", "rows", "after", "kind"), POLL_SEEN, ids=[c[0] for c in POLL_SEEN]
)
def test_the_poll_tier_sees(name: str, rows: list[Row], after: Observed, kind: str) -> None:
    clock = Clock()
    full = FullReads(clock)
    result = _run(
        [poll_tier(_snap(rows)), after], [0.0, 0.2], full, clock, trigger=True, before=_full(rows)
    )
    report = result.report
    assert name in (report.classes if kind == "class" else report.tokens)
    assert result.seen_by == "poll"
    assert result.timed_out is False
    assert len(full.at) == 1, "the full read once, after settle"


FULL_SEEN: list[tuple[str, dict[str, object], dict[str, object], str]] = [
    ("url_changed_in_place", {}, {"url": URL + "?q=red"}, "class"),
    ("windows_changed", {}, {"windows": (16, 32)}, "token"),
    (
        "own_value",
        {"acted_value": ValueFact(filled=False, digest="0")},
        {"acted_value": ValueFact(filled=True, digest="1")},
        "class",
    ),
]


@pytest.mark.parametrize(
    ("name", "before", "after", "kind"), FULL_SEEN, ids=[c[0] for c in FULL_SEEN]
)
def test_only_the_quiet_window_read_sees(
    name: str, before: dict[str, object], after: dict[str, object], kind: str
) -> None:
    clock = Clock()
    full = FullReads(clock, **after)
    result = _run(
        [poll_tier(_snap(BASE))], [0.0], full, clock, trigger=True, before=_full(BASE, **before)
    )
    report = result.report
    assert name in (report.classes if kind == "class" else report.tokens)
    assert result.seen_by == "full"
    assert result.timed_out is False
    assert full.at == [pytest.approx(SETTLE_S)], "found by the first quiet-window read, final"


def test_unknown_is_seen_by_the_poll_and_never_settles() -> None:
    clock = Clock()
    full = FullReads(clock)
    result = _run(
        [poll_tier(_snap(BASE)), poll_tier(_snap(BASE, stable=False))],
        [0.0, 0.2],
        full,
        clock,
        trigger=True,
    )
    assert result.seen_by == "poll"
    assert result.timed_out is True
