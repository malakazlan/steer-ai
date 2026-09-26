"""Top-level window enumeration via the Win32 API (ctypes, no third-party dependency)."""

from __future__ import annotations

import ctypes
import sys
import time
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
    # Explicit signatures: HWNDs are 64-bit and must not default to c_int.
    user32.EnumWindows.argtypes = (enum_proc, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
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


def wait_window_for_pid(
    pid: int, *, title_contains: str | None = None, timeout_s: float = 15.0
) -> WindowInfo:
    """First visible titled window owned by `pid`, polling until it appears."""
    deadline = time.monotonic() + timeout_s
    while True:
        for window in list_top_windows():
            if window.pid == pid and (title_contains is None or title_contains in window.title):
                return window
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no visible window for pid {pid} within {timeout_s} s")
        time.sleep(0.2)


def find_window(title_substring: str) -> WindowInfo | None:
    """First visible top-level window whose title contains the text, case-insensitive."""
    needle = title_substring.lower()
    for window in list_top_windows():
        if needle in window.title.lower():
            return window
    return None
