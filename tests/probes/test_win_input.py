"""SendInput wrapper: every injected event carries Steer's dwExtraInfo tag."""

import ctypes

import pytest

from steerai.probes.win_input import (
    STEER_EXTRA_INFO,
    build_mouse_input,
    move_cursor,
    pid_at_point,
    window_rect,
)

pytestmark = pytest.mark.windows


def test_mouse_input_carries_steer_tag_and_absolute_flags() -> None:
    record = build_mouse_input(dx=100, dy=200, flags=0x0001)
    assert record.type == 0  # INPUT_MOUSE
    assert record.union.mi.dwExtraInfo == STEER_EXTRA_INFO
    assert record.union.mi.dx == 100
    assert record.union.mi.dy == 200
    assert record.union.mi.dwFlags & 0x0001


def test_move_cursor_lands_on_requested_pixel() -> None:
    user32 = ctypes.windll.user32
    move_cursor(400, 300)
    point = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    assert (point.x, point.y) == (400, 300)


def test_pid_at_point_is_a_live_process() -> None:
    pid = pid_at_point(5, 5)
    assert pid > 0


def test_window_rect_of_desktop_is_screen_sized() -> None:
    user32 = ctypes.windll.user32
    left, top, right, bottom = window_rect(user32.GetDesktopWindow())
    assert (left, top) == (0, 0)
    assert right > 0
    assert bottom > 0
