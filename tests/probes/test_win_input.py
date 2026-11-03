"""SendInput wrapper: every injected event carries Steer's dwExtraInfo tag."""

import ctypes

import pytest

from steerai.probes.win_input import (
    STEER_EXTRA_INFO,
    VK_MENU,
    accessibility_filters_active,
    build_keyboard_input,
    build_mouse_input,
    cursor_pos,
    double_click_size_px,
    double_click_time_s,
    ensure_per_monitor_v2,
    foreground_hwnd,
    move_cursor,
    physical_buttons_down,
    physical_modifiers_down,
    pid_at_point,
    root_hwnd_at,
    system_dpi,
    window_rect,
)
from steerai.probes.windows import is_window

pytestmark = pytest.mark.windows


def test_mouse_input_carries_steer_tag_and_absolute_flags() -> None:
    record = build_mouse_input(dx=100, dy=200, flags=0x0001)
    assert record.type == 0  # INPUT_MOUSE
    assert record.union.mi.dwExtraInfo == STEER_EXTRA_INFO
    assert record.union.mi.dx == 100
    assert record.union.mi.dy == 200
    assert record.union.mi.dwFlags & 0x0001


def test_keyboard_input_carries_steer_tag_and_keyup_flag() -> None:
    down = build_keyboard_input(vk=VK_MENU, keyup=False)
    up = build_keyboard_input(vk=VK_MENU, keyup=True)
    assert down.type == 1  # INPUT_KEYBOARD
    assert down.union.ki.wVk == VK_MENU
    assert down.union.ki.dwFlags == 0
    assert up.union.ki.dwFlags & 0x0002
    assert up.union.ki.dwExtraInfo == STEER_EXTRA_INFO


def test_foreground_hwnd_is_a_window() -> None:
    assert foreground_hwnd() > 0


def test_system_dpi_is_at_least_96() -> None:
    assert system_dpi() >= 96


@pytest.mark.probe
def test_move_cursor_lands_on_requested_pixel() -> None:
    # The cursor is a desktop-global resource the physical mouse also writes: this is
    # deterministic only on an idle desktop (1/5 failures seen locally while the mouse was
    # in use). The executor guards itself with `cursor_mismatch`; run with -m probe.
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


def test_root_hwnd_at_the_desktop_centre_is_a_top_level_window() -> None:
    left, top, right, bottom = window_rect(ctypes.windll.user32.GetDesktopWindow())
    hwnd = root_hwnd_at((left + right) // 2, (top + bottom) // 2)
    assert hwnd != 0
    assert is_window(hwnd)
    assert ctypes.windll.user32.GetAncestor(hwnd, 2) == hwnd, "GA_ROOT of a root is itself"


def test_root_hwnd_at_a_point_outside_every_monitor_is_zero() -> None:
    assert root_hwnd_at(-100_000, -100_000) == 0


# ---- platform state the physical executor reads before every action (PX-01, 11, 16, 39) --------


def test_no_mouse_button_is_held_while_the_tests_run() -> None:
    assert physical_buttons_down() == []


def test_no_modifier_is_held_while_the_tests_run() -> None:
    assert physical_modifiers_down() == []


def test_accessibility_filters_report_a_bool_without_raising() -> None:
    assert isinstance(accessibility_filters_active(), bool)


@pytest.mark.probe
def test_cursor_pos_reads_back_the_moved_cursor() -> None:
    move_cursor(401, 301)
    assert cursor_pos() == (401, 301)


def test_double_click_time_is_within_the_settable_range() -> None:
    # SetDoubleClickTime accepts 0..5000 ms and GetDoubleClickTime answers 500 ms by default;
    # measured 0.5 s here (2026-11-04).
    assert 0.1 <= double_click_time_s() <= 5.0


def test_double_click_half_size_is_at_least_one_pixel() -> None:
    assert double_click_size_px() >= 1


def test_process_is_per_monitor_v2_aware() -> None:
    assert ensure_per_monitor_v2() is True
