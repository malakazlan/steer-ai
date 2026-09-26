"""Capture and input probe: pure helpers, plus Windows checks against the desktop window."""

import ctypes

import pytest

from steerai.probes.capture_input import (
    Capture,
    capture_window,
    image_is_blank,
    rects_agree,
    uia_rect_of_hwnd,
)
from steerai.probes.win_input import window_rect


def test_rects_agree_within_tolerance() -> None:
    assert rects_agree((0, 0, 100, 100), (1, 0, 100, 101), tolerance=1)
    assert not rects_agree((0, 0, 100, 100), (0, 0, 125, 125), tolerance=1)


def test_image_is_blank_detects_uniform_pixels() -> None:
    assert image_is_blank(bytes([0] * 400))
    assert image_is_blank(bytes([255] * 400))
    assert not image_is_blank(bytes([0, 0, 0, 255] * 50 + [200, 30, 30, 255] * 50))


def test_image_is_blank_rejects_empty() -> None:
    with pytest.raises(ValueError, match="no pixels"):
        image_is_blank(b"")


@pytest.mark.windows
def test_uia_rect_of_desktop_matches_win32_rect() -> None:
    hwnd = ctypes.windll.user32.GetDesktopWindow()
    assert rects_agree(uia_rect_of_hwnd(hwnd), window_rect(hwnd), tolerance=1)


@pytest.mark.windows
def test_capture_desktop_window_is_not_blank() -> None:
    hwnd = ctypes.windll.user32.GetDesktopWindow()
    capture = capture_window(hwnd, method="bitblt")
    assert isinstance(capture, Capture)
    assert capture.width > 0
    assert capture.height > 0
    assert capture.elapsed_ms > 0
    assert not capture.blank
