"""`observer.watch` (V2 §5, step 3b): after an action, poll the screen until it holds still, then
take the full read.

Pure: the reads, the clock and the sleep are injected. Two tiers ([V2.7], [V2.10]): `read` is
the poll, `full` adds every fact. Every read carries its tier and `observe` refuses to compare
two reads of different tiers, so each poll is compared with `before` seen through the poll tier.
A change is a seen class other than `none`, or a token other than `moved`; unread alone is never
a change (the final report still keeps unread facts, which bar `none`).

- A non-trigger verb ends once the screen has held still for `settle_s` since its last change
  (from the start when nothing changed): that full read is final, whatever it shows ([V2.10]).
- A trigger action (no read-back: a non-field click, an Enter) settles only after a strong
  report ([V2.8]). While none is seen, each expiry of the phase-1 quiet window takes one full
  read: strong is final; otherwise hold, at most one such read per quiet window, until the
  deadline ([V2.9]). A `frame_changed`-only change does not restart that quiet timer.
- A submit action ([V2.28] 1: anything through the Enter table, an Enter on a field, a query
  click, a submit click) holds the same way but ends early only on a CONCLUSIVE report: a URL
  move (`navigated`, `url_changed_in_place`), a strong class with a carrier, a dialog or a
  window change, or the typed field emptied. Anything else holds to the navigation guard
  (`SUBMIT_GUARD_S`, passed as the deadline). The caller evaluates the Enter table once, on the
  final report, so its rows 4, 8 and 9 come only from a read at the guard.

Slow reads ([V2.34]): every time is the time a read COMPLETED; a quiet window counts only
when two reads span it (the read that started it and a later one); the deadline never ends the
watch on a change no later read has seen (one more read, once). `Watched.read_cost` logs the
reads by tier.

The number of polls is bounded by `deadline_s / poll_s` (plus that one read) whatever the clock
does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from steerai.world.effects import EffectClass, EffectReport, Observed, observe, poll_tier
from steerai.world.reads import ReadCost, ReadMeter

Signature = tuple[object, ...]

WEAK_CLASSES = frozenset({"none", "focus", "own_state"})
"""[V2.8] what a trigger action's first change may be without ending phase 1."""
WEAK_TOKENS = frozenset({"moved", "frame_changed"})
SUBMIT_GUARD_S = 5.0
"""[V2.28] 1: the navigation guard a submit action holds to; provisional and unmeasured as a
hold (live run 2: all 13 early samples had navigated by the 5 s late read, the slowest real
navigation settled at 3.0 s). The re-run logs `Watched.conclusive_ms` to freeze it."""
URL_MOVED: frozenset[EffectClass] = frozenset({"navigated", "url_changed_in_place"})
CARRIED: frozenset[EffectClass] = frozenset({"content_changed", "revealed"})
"""The Enter table's strong classes (`loop2.query.STRONG`) that need a carrier to be
conclusive; the URL moves are conclusive alone."""


def strong(report: EffectReport) -> bool:
    """[V2.8] anything but `focus`, `own_state`, `frame_changed` (and `none`, `moved`)."""
    return bool(report.classes - WEAK_CLASSES) or bool(report.tokens - WEAK_TOKENS)


def changed(report: EffectReport) -> bool:
    """[V2.10] a seen change: a class other than `none` or a token other than `moved`; an
    unread fact alone is not one."""
    return bool(report.classes - {"none"}) or bool(report.tokens - {"moved"})


def conclusive(report: EffectReport, before: Observed, after: Observed) -> bool:
    """[V2.28] 1: a submit action's report that may end the watch early: a URL move, a strong
    class with a carrier, a dialog, a window change, or the typed field (`acted_value`) going
    from filled to empty. Anything else holds."""
    classes = report.classes
    if classes & URL_MOVED or "dialog_opened" in classes or "windows_changed" in report.tokens:
        return True
    if classes & CARRIED and report.carriers:
        return True
    old, new = before.acted_value, after.acted_value
    return old is not None and new is not None and old.filled and not new.filled


def _frame_only(report: EffectReport) -> bool:
    return not report.classes - {"none"} and report.tokens - {"moved"} == {"frame_changed"}


@dataclass(frozen=True, slots=True)
class Watched:
    report: EffectReport
    after: Observed
    first_change_ms: int | None
    """When a change was first seen; None when none was."""
    settled_ms: int | None
    """When the polls last saw the screen change; None when they never did."""
    timed_out: bool
    seen_by: Literal["poll", "full"] | None = None
    """[V2.9] which tier first saw a change: a poll, or a full read; None when neither did."""
    weak_reads: int = 0
    """Quiet-window full reads after a trigger that were not strong (for a submit, not
    conclusive): each is a point where a non-trigger verb would have ended ([V2.10] re-run
    question)."""
    conclusive_ms: int | None = None
    """[V2.28] 1: for a submit, when a read (a poll or a full read) first saw a conclusive
    report, so a re-run can log when the navigation arrived; None otherwise."""
    read_cost: ReadCost = field(default_factory=ReadCost)
    """[V2.34] the polls and full reads this watch made: count and milliseconds by tier."""


def _signature(seen: Observed) -> Signature:
    elements = tuple(
        (e.runtime_id, e.role, e.name, e.enabled, e.offscreen, e.caps)
        for e in seen.snapshot.elements
    )
    return (elements, seen.url, seen.focus, seen.acted_value)


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


@dataclass(slots=True)
class _Screen:
    """What the polls last saw, to tell a change from the last poll (or from `before`)."""

    judge: Callable[[Observed, Observed], EffectReport]
    last: Signature | None = None
    previous: Observed | None = None

    def update(self, seen: Observed, baseline: Observed) -> tuple[bool, bool]:
        """Whether `seen` changed, and whether the change restarts the phase-1 quiet timer (not
        `frame_changed`-only noise on a stable read)."""
        signature = _signature(seen)
        last = _signature(baseline) if self.last is None else self.last
        moved = signature != last or not seen.snapshot.stable
        noise = moved and self.previous is not None and _frame_only(self.judge(self.previous, seen))
        self.last, self.previous = signature, seen
        return moved, not noise or not seen.snapshot.stable


@dataclass(slots=True)
class _State:
    first: float | None = None
    seen_by: Literal["poll", "full"] | None = None
    changed_at: float | None = None
    quiet_from: float = 0.0
    probed_at: float | None = None
    weak_reads: int = 0
    conclusive_at: float | None = None
    since_change: int = 0
    """Polls since (and including) the one that last saw a change: two span a settle."""
    since_quiet: int = 0
    """Reads since (and including) the one that started the phase-1 quiet window."""

    def saw(self, report: EffectReport, at: float, tier: Literal["poll", "full"]) -> None:
        if self.first is None and changed(report):
            self.first, self.seen_by = at, tier

    def moved(self, at: float, *, quiet: bool) -> None:
        """The screen changed at `at`; `quiet` restarts the phase-1 quiet timer too (not for
        `frame_changed`-only noise on a stable read)."""
        self.changed_at = at
        self.since_change = 1
        if quiet:
            self.quiet_from, self.since_quiet = at, 1
        else:
            self.since_quiet += 1

    def still(self) -> None:
        """A poll that saw no change."""
        self.since_change += 1
        self.since_quiet += 1

    def track(self, at: float, *, moved: bool, quiet: bool) -> None:
        """A poll completed at `at`: `moved` when it saw a change, `quiet` when that change
        restarts the phase-1 quiet timer."""
        if moved:
            self.moved(at, quiet=quiet)
        else:
            self.still()

    def probed(self, at: float) -> None:
        """A quiet-window full read completed at `at` and was not final: the next quiet window
        starts there, with that read at its start."""
        self.probed_at, self.since_quiet = at, 1
        self.weak_reads += 1

    def ends(
        self, report: EffectReport, old: Observed, new: Observed, at: float, *, submit: bool
    ) -> bool:
        """Whether `report` (of `old` to `new`) ends a holding watch: strong for a trigger,
        conclusive for a submit, whose first conclusive read is recorded."""
        if not submit:
            return strong(report)
        if not conclusive(report, old, new):
            return False
        if self.conclusive_at is None:
            self.conclusive_at = at
        return True

    def result(
        self, report: EffectReport, after: Observed, cost: ReadCost, *, timed_out: bool
    ) -> Watched:
        return Watched(
            report,
            after,
            None if self.first is None else _ms(self.first),
            None if self.changed_at is None else _ms(self.changed_at),
            timed_out,
            self.seen_by,
            self.weak_reads,
            None if self.conclusive_at is None else _ms(self.conclusive_at),
            cost,
        )


def watch(  # noqa: PLR0913 - the action's facts and the injected clock, keyword-only
    read: Callable[[], Observed],
    before: Observed,
    *,
    acted: tuple[int, ...] | None = None,
    text: str | None = None,
    scope: tuple[int, ...] | None = None,
    url_fact: bool = True,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_s: float,
    settle_s: float,
    deadline_s: float,
    full: Callable[[Observed], Observed] | None = None,
    trigger: bool = False,
    submit: bool = False,
) -> Watched:
    """The effect of the action just sent, against `before` (see the module doc). `submit`
    ([V2.28] 1) holds for a conclusive report and needs `full`; `trigger` alone does not."""
    if poll_s <= 0 or settle_s < 0 or deadline_s <= 0:
        raise ValueError("watch needs a positive poll and deadline and a settle window >= 0")
    if submit and full is None:
        raise ValueError("a submit watch needs the full read")
    holding = trigger or submit

    def judge(old: Observed, new: Observed) -> EffectReport:
        return observe(old, new, acted=acted, text=text, scope=scope, url_fact=url_fact)

    def baseline(seen: Observed) -> Observed:
        return before if seen.tier == before.tier else poll_tier(before.snapshot)

    meter = ReadMeter(clock)

    def checked_read(seen: Observed) -> Observed:
        if full is None:
            return seen
        return meter.timed(lambda: full(seen))

    def final(seen: Observed) -> tuple[Observed, EffectReport]:
        checked = checked_read(seen)
        return checked, judge(baseline(checked), checked)

    start = clock()
    state = _State()
    screen = _Screen(judge)
    seen = before
    reread = False
    for _ in range(int(deadline_s / poll_s) + 2):
        seen = meter.timed(read)
        report = judge(baseline(seen), seen)
        elapsed = clock() - start
        state.saw(report, elapsed, "poll")
        polled_ends = holding and state.ends(report, baseline(seen), seen, elapsed, submit=submit)
        moved, restarts = screen.update(seen, baseline(seen))
        state.track(elapsed, moved=moved, quiet=restarts)
        stable = seen.snapshot.stable
        settled = elapsed - (state.changed_at or 0.0) >= settle_s and state.since_change >= 2
        if stable and settled and (not holding or (polled_ends and not submit)):
            seen, report = final(seen)
            state.saw(report, clock() - start, "full")
            return state.result(report, seen, meter.cost(), timed_out=False)
        quiet_since = max(state.quiet_from, -1.0 if state.probed_at is None else state.probed_at)
        quiet = elapsed - quiet_since >= settle_s and state.since_quiet >= 2
        if holding and full is not None and stable and quiet:
            checked = checked_read(seen)
            done = clock() - start
            checked_report = judge(before, checked)
            state.saw(checked_report, done, "full")
            if state.ends(checked_report, before, checked, done, submit=submit):
                return state.result(checked_report, checked, meter.cost(), timed_out=False)
            state.probed(done)
        if elapsed >= deadline_s:
            if state.since_change != 1 or reread:
                break
            reread = True  # [V2.34] the last poll saw a change: one more read before the guard
        sleep(poll_s)
    seen, report = final(seen)
    at = clock() - start
    state.saw(report, at, "full")
    if submit:
        state.ends(report, before, seen, at, submit=True)
    return state.result(report, seen, meter.cost(), timed_out=True)
