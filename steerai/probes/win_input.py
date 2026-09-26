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


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", _INPUTUNION))


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12  # ALT


def build_mouse_input(*, dx: int, dy: int, flags: int) -> INPUT:
    record = INPUT()
    record.type = _INPUT_MOUSE
    record.union.mi = MOUSEINPUT(dx, dy, 0, flags, 0, STEER_EXTRA_INFO)
    return record


def build_keyboard_input(*, vk: int, keyup: bool) -> INPUT:
    record = INPUT()
    record.type = _INPUT_KEYBOARD
    flags = _KEYEVENTF_KEYUP if keyup else 0
    record.union.ki = KEYBDINPUT(vk, 0, flags, 0, STEER_EXTRA_INFO)
    return record


def ensure_per_monitor_v2() -> bool:
    """Make this process Per-Monitor-V2 DPI aware. Must run before any UIA or Win32 rect call.

    Returns True when the context is (now) Per-Monitor-V2. Windows allows the context to be set
    once per process; a later call returns False with ERROR_ACCESS_DENIED, which is fine if the
    first call already chose V2.
    """
    if sys.platform != "win32":
        raise OSError("DPI awareness requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
        return True
    user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    current = user32.GetThreadDpiAwarenessContext()
    return bool(
        user32.AreDpiAwarenessContextsEqual(
            ctypes.c_void_p(current), _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    )


def _user32() -> ctypes.WinDLL:
    if sys.platform != "win32":
        raise OSError("SendInput requires Windows")
    ensure_per_monitor_v2()
    return ctypes.WinDLL("user32", use_last_error=True)


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


def key_tap(vk: int) -> None:
    user32 = _user32()
    _send(
        user32,
        build_keyboard_input(vk=vk, keyup=False),
        build_keyboard_input(vk=vk, keyup=True),
    )


def system_dpi() -> int:
    return int(_user32().GetDpiForSystem())


def foreground_hwnd() -> int:
    return int(_user32().GetForegroundWindow() or 0)


def acquire_foreground(hwnd: int) -> str:
    """Bring `hwnd` to the foreground; return the procedure that worked.

    Windows refuses SetForegroundWindow from a process that did not receive the last input.
    Procedures, cheapest first: direct call; a harmless ALT tap via SendInput (this process then
    counts as the last input source) then the call; AttachThreadInput to the foreground thread.
    """
    user32 = _user32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def is_foreground() -> bool:
        return int(user32.GetForegroundWindow() or 0) == hwnd

    if is_foreground():
        return "already"
    user32.SetForegroundWindow(hwnd)
    if is_foreground():
        return "direct"

    key_tap(VK_MENU)
    user32.SetForegroundWindow(hwnd)
    if is_foreground():
        return "alt-tap"

    current = user32.GetForegroundWindow()
    current_thread = user32.GetWindowThreadProcessId(current, None)
    own_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(own_thread, current_thread, True)
    try:
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.AttachThreadInput(own_thread, current_thread, False)
    if is_foreground():
        return "attach-thread-input"
    raise OSError(f"could not bring hwnd {hwnd:#x} to the foreground")


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
