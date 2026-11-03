"""Top-level window enumeration via the Win32 API."""

import os

import pytest

from steerai.probes.win_input import foreground_hwnd
from steerai.probes.windows import (
    WindowInfo,
    find_window,
    input_desktop_name,
    is_cloaked,
    is_hung,
    list_top_windows,
    process_name,
    wait_window_for_pid,
)

pytestmark = pytest.mark.windows


def test_lists_at_least_one_visible_titled_window() -> None:
    windows = list_top_windows()
    assert windows, "a desktop session always has at least one visible titled window"
    first = windows[0]
    assert isinstance(first, WindowInfo)
    assert first.hwnd > 0
    assert first.title
    assert first.pid > 0


def test_find_window_matches_title_case_insensitively() -> None:
    windows = list_top_windows()
    probe = windows[0].title[:4]
    found = find_window(probe.swapcase())
    assert found is not None
    assert probe.lower() in found.title.lower()


def test_wait_window_for_pid_times_out_for_windowless_pid() -> None:
    with pytest.raises(TimeoutError, match="no visible window for pid 0"):
        wait_window_for_pid(0, timeout_s=0.3)


def test_wait_window_for_pid_finds_an_existing_window() -> None:
    first = list_top_windows()[0]
    assert wait_window_for_pid(first.pid, timeout_s=1.0).pid == first.pid


def test_find_window_returns_none_when_absent() -> None:
    assert find_window("no window has this title 7f3a9c") is None


def test_listed_windows_are_never_cloaked() -> None:
    assert not any(is_cloaked(w.hwnd) for w in list_top_windows())


def test_process_name_of_a_listed_window_ends_with_exe() -> None:
    assert process_name(list_top_windows()[0].pid).lower().endswith(".exe")


def test_process_name_of_an_invalid_pid_is_empty() -> None:
    assert process_name(0) == ""


def test_minimized_flag_is_a_bool_for_every_window() -> None:
    assert all(isinstance(w.minimized, bool) for w in list_top_windows())


def test_window_info_minimized_defaults_to_false() -> None:
    assert WindowInfo(hwnd=1, title="t", pid=1).minimized is False


def test_is_cloaked_is_false_for_an_invalid_hwnd() -> None:
    assert is_cloaked(0) is False
    assert is_cloaked(0x7FFF_FFF1) is False


def test_process_name_of_the_current_process_is_a_bare_executable_name() -> None:
    name = process_name(os.getpid())
    assert name.lower().endswith(".exe")
    assert "\\" not in name
    assert "/" not in name


def test_listed_windows_have_unique_hwnds() -> None:
    hwnds = [w.hwnd for w in list_top_windows()]
    assert len(hwnds) == len(set(hwnds))


def test_input_desktop_is_the_default_one_in_an_interactive_session() -> None:
    # PX-17: "Winlogon" would mean the secure desktop, where SendInput is dropped.
    assert input_desktop_name() == "Default"


def test_foreground_window_is_not_hung() -> None:
    # PX-18: the window that just received input pumps messages.
    assert is_hung(foreground_hwnd()) is False
