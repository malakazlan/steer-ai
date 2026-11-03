"""SendInput wrapper (ctypes). Every event is tagged with Steer's dwExtraInfo value (design 5.9).

The process is made Per-Monitor-V2 DPI aware on first use so pixel coordinates match UIA rectangles.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Sequence
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
_KEYEVENTF_UNICODE = 0x0004
VK_MENU = 0x12  # ALT
_GA_ROOT = 2


def build_mouse_input(*, dx: int, dy: int, flags: int) -> INPUT:
    record = INPUT()
    record.type = _INPUT_MOUSE
    record.union.mi = MOUSEINPUT(dx, dy, 0, flags, 0, STEER_EXTRA_INFO)
    return record


_KEYEVENTF_EXTENDEDKEY = 0x0001


def build_keyboard_input(*, vk: int, keyup: bool, scan: int = 0, extended: bool = False) -> INPUT:
    record = INPUT()
    record.type = _INPUT_KEYBOARD
    flags = (_KEYEVENTF_KEYUP if keyup else 0) | (_KEYEVENTF_EXTENDEDKEY if extended else 0)
    record.union.ki = KEYBDINPUT(vk, scan, flags, 0, STEER_EXTRA_INFO)
    return record


def scan_code_for(vk: int) -> int:
    """Hardware scan code for a virtual key (MapVirtualKeyW), 0 when unavailable."""
    if sys.platform != "win32":
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    user32.MapVirtualKeyW.restype = wintypes.UINT
    return int(user32.MapVirtualKeyW(vk, 0))


_MODIFIER_VKS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win_left": 0x5B, "win_right": 0x5C}
_BUTTON_VKS = {"left": 0x01, "right": 0x02, "middle": 0x04, "x1": 0x05, "x2": 0x06}
_SM_CXDOUBLECLK, _SM_CYDOUBLECLK = 36, 37
_SPI_GETFILTERKEYS, _SPI_GETSTICKYKEYS = 0x0032, 0x003A
_FKF_FILTERKEYSON = _SKF_STICKYKEYSON = 0x0001


def physical_modifiers_down() -> list[str]:
    """Modifier keys the user is physically holding right now (GetAsyncKeyState high bit)."""
    user32 = _user32()
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return [name for name, vk in _MODIFIER_VKS.items() if user32.GetAsyncKeyState(vk) & 0x8000]


def physical_buttons_down() -> list[str]:
    """Mouse buttons the user is physically holding right now (PX-11)."""
    user32 = _user32()
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return [name for name, vk in _BUTTON_VKS.items() if user32.GetAsyncKeyState(vk) & 0x8000]


class _FILTERKEYS(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.UINT),
        ("dwFlags", wintypes.DWORD),
        ("iWaitMSec", wintypes.DWORD),
        ("iDelayMSec", wintypes.DWORD),
        ("iRepeatMSec", wintypes.DWORD),
        ("iBounceMSec", wintypes.DWORD),
    )


class _STICKYKEYS(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD))


def accessibility_filters_active() -> bool:
    """Filter Keys or Sticky Keys is switched on (`SPI_GETFILTERKEYS` / `SPI_GETSTICKYKEYS`):
    injected down/up pairs can be dropped or our modifiers latched for the user (PX-16)."""
    user32 = _user32()
    user32.SystemParametersInfoW.argtypes = (
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
    )
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    filter_keys = _FILTERKEYS(cbSize=ctypes.sizeof(_FILTERKEYS))
    sticky_keys = _STICKYKEYS(cbSize=ctypes.sizeof(_STICKYKEYS))
    if not user32.SystemParametersInfoW(
        _SPI_GETFILTERKEYS, filter_keys.cbSize, ctypes.byref(filter_keys), 0
    ):
        raise OSError(f"SPI_GETFILTERKEYS failed: error {ctypes.get_last_error()}")
    if not user32.SystemParametersInfoW(
        _SPI_GETSTICKYKEYS, sticky_keys.cbSize, ctypes.byref(sticky_keys), 0
    ):
        raise OSError(f"SPI_GETSTICKYKEYS failed: error {ctypes.get_last_error()}")
    return bool(filter_keys.dwFlags & _FKF_FILTERKEYSON) or bool(
        sticky_keys.dwFlags & _SKF_STICKYKEYSON
    )


def double_click_time_s() -> float:
    user32 = _user32()
    user32.GetDoubleClickTime.restype = wintypes.UINT
    return int(user32.GetDoubleClickTime()) / 1000


def double_click_size_px() -> int:
    """Half the double-click rectangle: two clicks closer than this (and within the time) form a
    double click (`SM_CXDOUBLECLK` / `SM_CYDOUBLECLK`)."""
    user32 = _user32()
    width = int(user32.GetSystemMetrics(_SM_CXDOUBLECLK))
    height = int(user32.GetSystemMetrics(_SM_CYDOUBLECLK))
    return max(width, height) // 2


def cursor_pos() -> tuple[int, int]:
    user32 = _user32()
    user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    user32.GetCursorPos.restype = wintypes.BOOL
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise OSError(f"GetCursorPos failed: error {ctypes.get_last_error()}")
    return int(point.x), int(point.y)


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


def dpi_context_is_per_monitor_v2() -> bool:
    """Pure query: is this thread's DPI awareness context Per-Monitor-V2 right now. Unlike
    `ensure_per_monitor_v2` it sets nothing, so an executor can detect that the context was
    chosen wrongly or too late (design 5.9 [v0.6] ordering rule, PX-01)."""
    if sys.platform != "win32":
        raise OSError("DPI awareness requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.AreDpiAwarenessContextsEqual.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    user32.AreDpiAwarenessContextsEqual.restype = wintypes.BOOL
    current = user32.GetThreadDpiAwarenessContext()
    return bool(
        user32.AreDpiAwarenessContextsEqual(
            ctypes.c_void_p(current), _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    )


def _user32() -> ctypes.WinDLL:
    """user32 with explicit signatures: HWNDs are 64-bit and must not default to c_int."""
    if sys.platform != "win32":
        raise OSError("SendInput requires Windows")
    ensure_per_monitor_v2()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetDpiForSystem.restype = wintypes.UINT
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
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


def build_keyboard_unicode(unit: int, *, keyup: bool) -> INPUT:
    """A KEYEVENTF_UNICODE event for one UTF-16 code unit (layout independent)."""
    record = INPUT()
    record.type = _INPUT_KEYBOARD
    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if keyup else 0)
    record.union.ki = KEYBDINPUT(0, unit, flags, 0, STEER_EXTRA_INFO)
    return record


def send_inputs(records: Sequence[INPUT]) -> None:
    """Inject a batch of tagged events; raises when Windows accepted fewer than sent."""
    if records:
        _send(_user32(), *records)


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
    user32.IsHungAppWindow.argtypes = (wintypes.HWND,)
    user32.IsHungAppWindow.restype = wintypes.BOOL
    if not current or user32.IsHungAppWindow(current):
        # Attaching to a non-pumping thread blocks indefinitely (PX-24): skip the rung.
        raise OSError(f"could not bring hwnd {hwnd:#x} to the foreground")
    current_thread = user32.GetWindowThreadProcessId(current, None)
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
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


def root_hwnd_at(x: int, y: int) -> int:
    """Top-level window (`GetAncestor(GA_ROOT)`) under a screen pixel, 0 when there is none.

    The covered check (design 5.9 [v0.7]) compares this with the target's root hwnd: a child of
    the target window owns the pixel in the normal case, another root means a covering window.
    """
    user32 = _user32()
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not hwnd:
        return 0
    return int(user32.GetAncestor(hwnd, _GA_ROOT) or 0)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    user32 = _user32()
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect failed for hwnd {hwnd:#x}: error {ctypes.get_last_error()}")
    return rect.left, rect.top, rect.right, rect.bottom
