"""`observer.watch` (V2 §5, step 3b): after an action, read until the first change, then until the
report holds for the settle window, or until the deadline. Pure: the read, the clock and the
sleep are injected; the report is always `observe(before, last read)`."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from steerai.world.effects import Observed
from steerai.world.watch import watch
from tests.world.test_effects_conditions import BASE, FIELD, Row, _snap

ITEM2: Row = (5, "ListItem", "Red shoes", (9, 5), 3)
ITEM3: Row = (6, "ListItem", "Red boots", (9, 6), 3)


def _obs(rows: Sequence[Row], *, stable: bool = True) -> Observed:
    return Observed(snapshot=_snap(rows, stable=stable), url="https://shop.test/", windows=(16,))


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _reader(frames: list[Observed], clock: Clock, at: list[float]) -> Callable[[], Observed]:
    """Each frame becomes the read from its time on."""

    def read() -> Observed:
        index = max(i for i, t in enumerate(at) if t <= clock.t)
        return frames[index]

    return read


def test_nothing_changes_until_the_deadline_is_none_and_timed_out() -> None:
    clock = Clock()
    before = _obs(BASE)
    result = watch(
        lambda: before,
        before,
        acted=FIELD[3],
        clock=clock.now,
        sleep=clock.sleep,
        poll_s=0.1,
        settle_s=0.3,
        deadline_s=1.0,
    )
    assert result.report.classes == frozenset({"none"})
    assert result.first_change_ms is None
    assert result.timed_out is True


def test_a_change_then_quiet_ends_after_the_settle_window() -> None:
    clock = Clock()
    before = _obs(BASE)
    after = _obs([*BASE, ITEM2])
    read = _reader([before, after], clock, [0.0, 0.25])
    result = watch(
        read,
        before,
        acted=FIELD[3],
        clock=clock.now,
        sleep=clock.sleep,
        poll_s=0.1,
        settle_s=0.3,
        deadline_s=5.0,
    )
    assert "content_changed" in result.report.classes
    assert result.first_change_ms is not None
    assert 250 <= result.first_change_ms <= 400
    assert result.timed_out is False
    assert clock.t < 1.0, "it stops once the settle window holds"


def test_a_second_change_restarts_the_settle_window_and_is_in_the_report() -> None:
    clock = Clock()
    before = _obs(BASE)
    read = _reader(
        [before, _obs([*BASE, ITEM2]), _obs([*BASE, ITEM2, ITEM3])], clock, [0.0, 0.2, 0.4]
    )
    result = watch(
        read,
        before,
        acted=FIELD[3],
        text="red boots",
        clock=clock.now,
        sleep=clock.sleep,
        poll_s=0.1,
        settle_s=0.3,
        deadline_s=5.0,
    )
    assert "results" in result.report.carriers, "the report is against the last read"
    assert result.settled_ms is not None
    assert result.settled_ms >= 400


def test_an_unstable_read_never_settles_and_the_deadline_gives_unknown() -> None:
    clock = Clock()
    before = _obs(BASE)
    result = watch(
        lambda: _obs([*BASE, ITEM2], stable=False),
        before,
        acted=FIELD[3],
        clock=clock.now,
        sleep=clock.sleep,
        poll_s=0.1,
        settle_s=0.3,
        deadline_s=1.0,
    )
    assert result.report.classes == frozenset({"unknown"})
    assert result.timed_out is True


def test_the_read_count_is_bounded_whatever_the_clock_does() -> None:
    reads = []
    before = _obs(BASE)

    def read() -> Observed:
        reads.append(1)
        return before

    watch(
        read,
        before,
        acted=FIELD[3],
        clock=lambda: 0.0,
        sleep=lambda _s: None,
        poll_s=0.1,
        settle_s=0.3,
        deadline_s=1.0,
    )
    assert len(reads) <= 11 + 1
