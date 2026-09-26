"""SendInput wrapper (ctypes). Every event is tagged with Steer's dwExtraInfo value (design 5.9).

The process is made Per-Monitor-V2 DPI aware on first use so pixel coordinates match UIA rectangles.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

STEER_EXTRA_INFO = 0x5354_4545  # "STEE"
_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN = 76, 77
_SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 78, 79
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT),)


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", _INPUTUNION))


def build_mouse_input(*, dx: int, dy: int, flags: int) -> INPUT:
    record = INPUT()
    record.type = _INPUT_MOUSE
    record.union.mi = MOUSEINPUT(dx, dy, 0, flags, 0, STEER_EXTRA_INFO)
    return record


def _user32() -> ctypes.WinDLL:
    if sys.platform != "win32":
        raise OSError("SendInput requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    return user32


def _to_absolute(user32: ctypes.WinDLL, x: int, y: int) -> tuple[int, int]:
    """Pixel → 0..65535 virtual-screen coordinates, rounding to the pixel centre."""
    vx = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    ax = ((x - vx) * 65536 + 32768) // vw
    ay = ((y - vy) * 65536 + 32768) // vh
    return ax, ay


def _send(user32: ctypes.WinDLL, *records: INPUT) -> None:
    array = (INPUT * len(records))(*records)
    sent = user32.SendInput(len(records), array, ctypes.sizeof(INPUT))
    if sent != len(records):
        raise OSError(f"SendInput sent {sent}/{len(records)}: error {ctypes.get_last_error()}")


def move_cursor(x: int, y: int) -> None:
    user32 = _user32()
    ax, ay = _to_absolute(user32, x, y)
    flags = _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
    _send(user32, build_mouse_input(dx=ax, dy=ay, flags=flags))


def click_at(x: int, y: int) -> None:
    user32 = _user32()
    ax, ay = _to_absolute(user32, x, y)
    base = _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
    _send(
        user32,
        build_mouse_input(dx=ax, dy=ay, flags=base | _MOUSEEVENTF_MOVE),
        build_mouse_input(dx=ax, dy=ay, flags=base | _MOUSEEVENTF_LEFTDOWN),
        build_mouse_input(dx=ax, dy=ay, flags=base | _MOUSEEVENTF_LEFTUP),
    )


def pid_at_point(x: int, y: int) -> int:
    """Process id owning the top-level window under a screen pixel (0 if none)."""
    user32 = _user32()
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not hwnd:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    user32 = _user32()
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect failed for hwnd {hwnd:#x}: error {ctypes.get_last_error()}")
    return rect.left, rect.top, rect.right, rect.bottom
