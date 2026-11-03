"""Readback poll (design 5.9 [v0.7], [v1.1]): 50 ms steps, bounded by the earlier of a 400 ms
monotonic deadline and 8 steps, so a blocking cross-process read cannot stretch it."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import steerai.action.readback as mod
from steerai.action.dispositions import Delivery
from steerai.action.readback import Readback, poll_state, poll_until
from steerai.errors import ErrorCode


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep))
    return fake


class _Counter:
    def __init__(self, value: bool = False) -> None:
        self.value = value
        self.reads = 0

    def __call__(self) -> bool:
        self.reads += 1
        return self.value


class _ReaderDiedError(RuntimeError):
    pass


def test_poll_returns_true_as_soon_as_the_condition_holds(clock: _Clock) -> None:
    reads = iter([False, False, True])
    assert poll_until(lambda: next(reads)) is True
    assert clock.sleeps == [0.05, 0.05]


def test_poll_gives_up_at_the_deadline(clock: _Clock) -> None:
    condition = _Counter()
    assert poll_until(condition) is False
    assert clock.now == pytest.approx(0.4)
    assert len(clock.sleeps) == 8
    assert condition.reads == 9


def test_poll_rejects_bad_parameters(clock: _Clock) -> None:
    with pytest.raises(ValueError, match="step_s"):
        poll_until(lambda: True, step_s=0.0)
    with pytest.raises(ValueError, match="step_s"):
        poll_until(lambda: True, step_s=-0.05)
    with pytest.raises(ValueError, match="deadline_s"):
        poll_until(lambda: True, deadline_s=-1.0)
    with pytest.raises(ValueError, match="max_steps"):
        poll_until(lambda: True, max_steps=-1)


@pytest.mark.parametrize("held", [True, False])
def test_deadline_zero_reads_the_condition_exactly_once_without_sleeping(
    clock: _Clock, held: bool
) -> None:
    condition = _Counter(held)
    assert poll_until(condition, deadline_s=0.0) is held
    assert condition.reads == 1
    assert clock.sleeps == []


def test_max_steps_zero_reads_once_without_sleeping(clock: _Clock) -> None:
    condition = _Counter()
    assert poll_until(condition, max_steps=0) is False
    assert condition.reads == 1
    assert clock.sleeps == []


def test_step_larger_than_the_deadline_is_clipped_to_the_deadline(clock: _Clock) -> None:
    condition = _Counter()
    assert poll_until(condition, step_s=1.0, deadline_s=0.4) is False
    assert condition.reads == 2
    assert clock.sleeps == [0.4]


def test_step_that_does_not_divide_the_deadline_never_overshoots_it(clock: _Clock) -> None:
    condition = _Counter()
    assert poll_until(condition, step_s=0.25, deadline_s=0.4) is False
    assert clock.sleeps == pytest.approx([0.25, 0.15])
    assert clock.now == pytest.approx(0.4)
    assert condition.reads == 3


def test_deadline_of_exactly_one_step_reads_twice(clock: _Clock) -> None:
    condition = _Counter()
    assert poll_until(condition, deadline_s=0.05) is False
    assert condition.reads == 2
    assert clock.sleeps == [0.05]


def test_deadline_of_half_a_step_reads_twice_with_a_clipped_sleep(clock: _Clock) -> None:
    condition = _Counter()
    assert poll_until(condition, deadline_s=0.025) is False
    assert condition.reads == 2
    assert clock.sleeps == [0.025]


def test_step_count_bound_stops_the_poll_before_the_deadline(clock: _Clock) -> None:
    # 0.01 s steps would allow 40 reads inside 0.4 s; the count bound stops at 8 steps.
    condition = _Counter()
    assert poll_until(condition, step_s=0.01, deadline_s=0.4) is False
    assert len(clock.sleeps) == 8
    assert condition.reads == 9
    assert clock.now == pytest.approx(0.08)


def test_a_read_that_blocks_past_the_deadline_ends_the_poll(clock: _Clock) -> None:
    # A cross-process read can block up to the UIA transaction timeout (5 s); once it returns
    # past the deadline no further sleep or read happens.
    reads = 0

    def slow() -> bool:
        nonlocal reads
        reads += 1
        clock.now += 0.5
        return False

    assert poll_until(slow) is False
    assert reads == 1
    assert clock.sleeps == []


def test_a_raising_condition_propagates_with_its_exact_type_and_stops_polling(
    clock: _Clock,
) -> None:
    reads = 0

    def condition() -> bool:
        nonlocal reads
        reads += 1
        raise _ReaderDiedError("reader died")

    with pytest.raises(_ReaderDiedError, match="reader died"):
        poll_until(condition)
    assert reads == 1
    assert clock.sleeps == []


def test_real_clock_poll_elapses_at_least_the_deadline() -> None:
    start = time.monotonic()
    assert poll_until(lambda: False, step_s=0.005, deadline_s=0.02) is False
    assert time.monotonic() - start >= 0.02 - 1e-6


# ---- poll_state: verified, settled on a wrong state, or still moving ----------------------


def test_poll_state_is_verified_once_the_read_matches(clock: _Clock) -> None:
    reads = iter([0, 0, 1])
    result = poll_state(lambda: next(reads), 1)
    assert result == Readback(verified=True, settled=False)
    assert clock.sleeps == [0.05, 0.05]


def test_poll_state_settles_when_the_last_two_reads_agree_on_a_wrong_state(
    clock: _Clock,
) -> None:
    result = poll_state(lambda: 0, 1)
    assert result == Readback(verified=False, settled=True)
    assert clock.now == pytest.approx(0.4)


def test_poll_state_is_unsettled_while_the_reads_keep_changing(clock: _Clock) -> None:
    values = iter([0, 2] * 10)
    result = poll_state(lambda: next(values), 1)
    assert result == Readback(verified=False, settled=False)


def test_poll_state_needs_two_samples_to_settle(clock: _Clock) -> None:
    assert poll_state(lambda: 0, 1, deadline_s=0.0) == Readback(verified=False, settled=False)


def test_readback_maps_to_delivery_and_code() -> None:
    assert Readback(verified=True, settled=False).delivery is Delivery.DELIVERED_VERIFIED
    assert Readback(verified=True, settled=False).code is None
    assert Readback(verified=False, settled=True).delivery is Delivery.DELIVERED_UNVERIFIED
    assert Readback(verified=False, settled=True).code is ErrorCode.VERIFY_FAILED
    assert Readback(verified=False, settled=False).code is ErrorCode.VERIFY_TIMEOUT


def test_readback_cannot_be_both_verified_and_settled_wrong() -> None:
    with pytest.raises(ValueError, match="settled"):
        Readback(verified=True, settled=True)
