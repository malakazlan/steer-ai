"""Actionability (design 5.9 [v0.7]): visible, stable, enabled, receives events, same integrity."""

import os
from types import SimpleNamespace

import pytest

import steerai.action.actionability as mod
from steerai.action.actionability import (
    Actionability,
    check_actionable,
    integrity_allows_input,
    process_integrity_level,
    rects_stable,
    sample_stable_rect,
)
from steerai.errors import ErrorCode, SteerError
from steerai.world.elements import Element

BOX = (100, 100, 200, 140)


def _el(**kw: object) -> Element:
    base: dict[str, object] = {"id": 1, "depth": 0, "role": "Button", "name": "Send", "box": BOX}
    base.update(kw)
    return Element(**base)  # type: ignore[arg-type]


def test_rects_stable_within_half_a_pixel() -> None:
    assert rects_stable((0, 0, 10, 10), (0, 0, 10, 10)) is True
    assert rects_stable((0, 0, 10, 10), (0, 0, 10, 11)) is False


def test_check_rejects_offscreen_disabled_and_zero_area() -> None:
    for element, reason in (
        (_el(offscreen=True), "off-screen"),
        (_el(enabled=False), "disabled"),
        (_el(box=(5, 5, 5, 40)), "no area"),
    ):
        with pytest.raises(SteerError) as info:
            check_actionable(element, owner_pid_at_centre=42, target_pid=42)
        assert info.value.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
        assert reason in info.value.message


def test_check_rejects_a_covered_element() -> None:
    with pytest.raises(SteerError, match="covered"):
        check_actionable(_el(), owner_pid_at_centre=99, target_pid=42)


def test_check_returns_the_centre_for_an_actionable_element() -> None:
    result = check_actionable(_el(), owner_pid_at_centre=42, target_pid=42)
    assert isinstance(result, Actionability)
    assert result.centre == (150, 120)


def test_integrity_blocks_input_into_a_higher_level() -> None:
    assert integrity_allows_input(caller_level=0x2000, target_level=0x3000) is False
    assert integrity_allows_input(caller_level=0x2000, target_level=0x2000) is True
    assert integrity_allows_input(caller_level=0x3000, target_level=0x2000) is True


def test_unknown_target_integrity_is_best_effort_not_assumed_lower() -> None:
    assert integrity_allows_input(caller_level=0x2000, target_level=None) is True


def test_sample_stable_rect_accepts_agreeing_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(mod, "time", SimpleNamespace(sleep=sleeps.append))
    reads = iter([(0, 0, 10, 10), (0, 0, 10, 10)])
    assert sample_stable_rect(lambda: next(reads)) == (0, 0, 10, 10)
    assert sleeps == [0.034]


def test_sample_stable_rect_rejects_a_moving_element(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "time", SimpleNamespace(sleep=lambda _s: None))
    reads = iter([(0, 0, 10, 10), (3, 0, 13, 10)])
    with pytest.raises(SteerError) as info:
        sample_stable_rect(lambda: next(reads))
    assert info.value.code is ErrorCode.UNSTABLE_UI


@pytest.mark.windows
def test_own_process_integrity_level_is_a_known_level() -> None:
    assert process_integrity_level(os.getpid()) in (0x1000, 0x2000, 0x3000, 0x4000)


@pytest.mark.windows
def test_integrity_level_of_an_invalid_pid_is_none() -> None:
    assert process_integrity_level(0) is None
