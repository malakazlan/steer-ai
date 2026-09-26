"""Top-level window enumeration via the Win32 API."""

import pytest

from steerai.probes.windows import WindowInfo, find_window, list_top_windows

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


def test_find_window_returns_none_when_absent() -> None:
    assert find_window("no window has this title 7f3a9c") is None
