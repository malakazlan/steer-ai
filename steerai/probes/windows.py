"""Top-level window enumeration via the Win32 API (ctypes, no third-party dependency)."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int


def list_top_windows() -> list[WindowInfo]:
    """Visible, titled top-level windows in z-order (foreground first)."""
    if sys.platform != "win32":
        raise OSError("window enumeration requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[WindowInfo] = []

    def on_window(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(WindowInfo(hwnd=int(hwnd), title=buffer.value, pid=pid.value))
        return True

    user32.EnumWindows(enum_proc(on_window), 0)
    return found


def find_window(title_substring: str) -> WindowInfo | None:
    """First visible top-level window whose title contains the text, case-insensitive."""
    needle = title_substring.lower()
    for window in list_top_windows():
        if needle in window.title.lower():
            return window
    return None
