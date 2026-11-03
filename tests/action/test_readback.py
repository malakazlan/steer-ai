"""Readback poll (design 5.9 [v0.7]): 50 ms steps up to 400 ms against a verb's postcondition."""

from types import SimpleNamespace

import pytest

import steerai.action.readback as mod
from steerai.action.dispositions import Delivery
from steerai.action.readback import poll_until, readback_delivery


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


def test_poll_returns_true_as_soon_as_the_condition_holds(clock: _Clock) -> None:
    reads = iter([False, False, True])
    assert poll_until(lambda: next(reads)) is True
    assert clock.sleeps == [0.05, 0.05]


def test_poll_gives_up_at_the_deadline(clock: _Clock) -> None:
    assert poll_until(lambda: False) is False
    assert clock.now == pytest.approx(0.4)
    assert len(clock.sleeps) == 8


def test_poll_rejects_bad_parameters(clock: _Clock) -> None:
    with pytest.raises(ValueError, match="step_s"):
        poll_until(lambda: True, step_s=0.0)
    with pytest.raises(ValueError, match="deadline_s"):
        poll_until(lambda: True, deadline_s=-1.0)


def test_readback_delivery_maps_outcomes() -> None:
    assert readback_delivery(verified=True) is Delivery.DELIVERED_VERIFIED
    assert readback_delivery(verified=False) is Delivery.DELIVERED_UNVERIFIED
    assert readback_delivery(verified=None) is Delivery.DELIVERED_UNVERIFIED
