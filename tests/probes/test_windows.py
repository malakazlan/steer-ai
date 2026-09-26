"""Top-level window enumeration via the Win32 API."""

import pytest

from steerai.probes.windows import WindowInfo, find_window, list_top_windows, wait_window_for_pid

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
