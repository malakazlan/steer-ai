"""Actionability (design 5.9 [v0.7]): visible, stable, enabled, receives events, same integrity."""

import os
from types import SimpleNamespace

import pytest

import steerai.action.actionability as mod
from steerai.action.actionability import (
    Actionability,
    centre_of,
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
            check_actionable(element, root_hwnd_at_centre=42, target_root_hwnd=42)
        assert info.value.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
        assert reason in info.value.message


def test_check_rejects_a_covered_element() -> None:
    with pytest.raises(SteerError, match="covered"):
        check_actionable(_el(), root_hwnd_at_centre=99, target_root_hwnd=42)


def test_check_returns_the_centre_for_an_actionable_element() -> None:
    result = check_actionable(_el(), root_hwnd_at_centre=42, target_root_hwnd=42)
    assert isinstance(result, Actionability)
    assert result.centre == (150, 120)


def test_integrity_blocks_input_into_a_higher_level() -> None:
    assert integrity_allows_input(caller_level=0x2000, target_level=0x3000) is False
    assert integrity_allows_input(caller_level=0x2000, target_level=0x2000) is True
    assert integrity_allows_input(caller_level=0x3000, target_level=0x2000) is True


def test_unknown_target_integrity_blocks_input() -> None:
    # Measured (probe uipi-integrity-2026-11-03): unreadable means higher than ours.
    assert integrity_allows_input(caller_level=0x2000, target_level=None) is False


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


@pytest.mark.parametrize(
    ("box", "expected"),
    [
        ((0, 0, 1, 1), (0, 0)),
        ((0, 0, 2, 1), (1, 0)),
        ((100, 100, 101, 102), (100, 101)),
        ((-201, -100, -100, -50), (-151, -75)),
        ((7, 7, 8, 8), (7, 7)),
    ],
)
def test_centre_is_strictly_inside_the_half_open_box(
    box: tuple[int, int, int, int], expected: tuple[int, int]
) -> None:
    cx, cy = centre_of(box)
    assert (cx, cy) == expected
    assert box[0] <= cx < box[2]
    assert box[1] <= cy < box[3]


@pytest.mark.parametrize("box", [(10, 10, 5, 40), (0, 0, 10, 0), (0, 0, 0, 0), (5, 9, 50, 9)])
def test_check_rejects_negative_zero_width_and_zero_height_boxes(
    box: tuple[int, int, int, int],
) -> None:
    with pytest.raises(SteerError) as info:
        check_actionable(_el(box=box), root_hwnd_at_centre=42, target_root_hwnd=42)
    assert info.value.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert "no area" in info.value.message


def test_one_pixel_box_is_actionable_at_its_only_pixel() -> None:
    result = check_actionable(_el(box=(7, 7, 8, 8)), root_hwnd_at_centre=42, target_root_hwnd=42)
    assert result.centre == (7, 7)


def test_offscreen_wins_over_other_reasons() -> None:
    with pytest.raises(SteerError, match="off-screen"):
        check_actionable(
            _el(offscreen=True, enabled=False, box=(0, 0, 0, 0)),
            root_hwnd_at_centre=1,
            target_root_hwnd=42,
        )


@pytest.mark.parametrize(
    ("a", "b", "tolerance", "stable"),
    [
        ((0, 0, 10, 10), (1, 0, 10, 10), 0.5, False),
        ((0, 0, 10, 10), (-1, 0, 10, 10), 0.5, False),
        ((0, 0, 10, 10), (1, 0, 10, 10), 1.0, True),
        ((0, 0, 10, 10), (2, 0, 10, 10), 1.0, False),
        ((0, 0, 10, 10), (0, 0, 10, 10), 0.0, True),
        ((0, 0, 10, 10), (0, 0, 10, 11), 0.0, False),
    ],
)
def test_rects_stable_tolerance_boundaries(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
    tolerance: float,
    stable: bool,
) -> None:
    assert rects_stable(a, b, tolerance=tolerance) is stable


@pytest.mark.parametrize(
    ("caller", "target", "allowed"),
    [
        (0x0000, 0x1000, False),
        (0x1000, 0x1000, True),
        (0x1000, 0x2000, False),
        (0x3000, 0x4000, False),
        (0x4000, 0x4000, True),
        (0x4000, 0x1000, True),
        (0x2000, 0x0000, True),
    ],
)
def test_integrity_boundaries_untrusted_to_system(caller: int, target: int, allowed: bool) -> None:
    assert integrity_allows_input(caller_level=caller, target_level=target) is allowed


def test_sample_stable_rect_propagates_the_reader_error_before_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(mod, "time", SimpleNamespace(sleep=sleeps.append))

    def read() -> tuple[int, int, int, int]:
        raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "rect read timed out")

    with pytest.raises(SteerError) as info:
        sample_stable_rect(read)
    assert info.value.code is ErrorCode.PERCEPTION_TIMEOUT
    assert sleeps == []


def test_sample_stable_rect_honours_the_gap_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(mod, "time", SimpleNamespace(sleep=sleeps.append))
    reads = iter([(0, 0, 10, 10), (0, 0, 10, 10)])
    sample_stable_rect(lambda: next(reads), gap_s=0.0)
    assert sleeps == [0.0]
