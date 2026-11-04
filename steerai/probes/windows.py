"""Top-level window enumeration via the Win32 API (ctypes, no third-party dependency)."""

from __future__ import annotations

import ctypes
import functools
import os
import sys
import time
from collections.abc import Callable, Mapping
from ctypes import wintypes
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    minimized: bool = False
    """Minimized windows sit at (-32000, -32000) with an empty UIA rect; they are not drivable."""


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
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    found: list[WindowInfo] = []

    def on_window(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or is_cloaked(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=buffer.value,
                pid=pid.value,
                minimized=bool(user32.IsIconic(hwnd)),
            )
        )
        return True

    user32.EnumWindows(enum_proc(on_window), 0)
    return found


def is_cloaked(hwnd: int) -> bool:
    """True for windows DWM hides although IsWindowVisible says visible (UWP hosts, other desktops).

    Such windows have a Win32 rect but an empty UIA tree and rect; they are never targets.
    """
    if sys.platform != "win32":
        raise OSError("DWM attributes require Windows")
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    dwmapi.DwmGetWindowAttribute.argtypes = (
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    dwmwa_cloaked = 14
    cloaked = wintypes.DWORD(0)
    result = dwmapi.DwmGetWindowAttribute(
        hwnd, dwmwa_cloaked, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return result == 0 and cloaked.value != 0


def parent_pids(pid: int, *, limit: int = 16) -> list[int]:
    """The parent chain of a process from a Toolhelp snapshot (nearest first), stopped at the
    limit or at a cycle; a pid whose parent is gone ends the chain."""
    if sys.platform != "win32":
        raise OSError("process lookup requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, ctypes.c_void_p(-1).value):
        return []
    parents: dict[int, int] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parent_chain(pid, parents, process_start_time, limit=limit)


def parent_chain(
    pid: int,
    parents: Mapping[int, int],
    started: Callable[[int], int | None],
    *,
    limit: int = 16,
) -> list[int]:
    """Walk the parent map nearest first; stop at the root, at a cycle, at the limit, and at a
    parent whose start time is unknown or later than its child's: a parent cannot start after
    its child, so that pid was reused by an unrelated process (RN-15 follow-up)."""
    chain: list[int] = []
    current = pid
    child_started = started(pid)
    while len(chain) < limit:
        parent = parents.get(current)
        if parent is None or parent == 0 or parent in chain or parent == pid:
            break
        parent_started = started(parent)
        if child_started is None or parent_started is None or parent_started > child_started:
            break
        chain.append(parent)
        current, child_started = parent, parent_started
    return chain


def process_start_time(pid: int) -> int | None:
    """The process creation time as a FILETIME count (100 ns units), or None when the process
    cannot be opened."""
    if sys.platform != "win32":
        raise OSError("process lookup requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, created, exited, kernel, user):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


class _ProcessEntry(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    )


def console_exclusions() -> tuple[set[int], set[int]]:
    """(hwnds, pids) a runner must never target: its console window and the window that owns
    it (under ConPTY the console is a hidden pseudo-window owned by the terminal, so
    `GetConsoleWindow` alone misses Windows Terminal), every process attached to the console,
    this process and its parent chain (RN-15)."""
    if sys.platform != "win32":
        raise OSError("console lookup requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.GetConsoleProcessList.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
    kernel32.GetConsoleProcessList.restype = wintypes.DWORD
    user32 = _user32()
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    hwnds: set[int] = set()
    pids: set[int] = {os.getpid(), *parent_pids(os.getpid())}
    console = kernel32.GetConsoleWindow()
    if console:
        hwnds.add(int(console))
        pids.add(window_pid(int(console)))
        owner = user32.GetAncestor(console, 3)  # GA_ROOTOWNER
        if owner:
            hwnds.add(int(owner))
            pids.add(window_pid(int(owner)))
    buffer = (wintypes.DWORD * 64)()
    count = kernel32.GetConsoleProcessList(buffer, 64)
    if count > 64:
        buffer = (wintypes.DWORD * count)()
        count = kernel32.GetConsoleProcessList(buffer, count)
    pids.update(int(buffer[i]) for i in range(min(count, len(buffer))))
    pids.discard(0)
    return hwnds, pids


def process_alive(pid: int) -> bool:
    """Whether a process with this id is still running (a handle opens and the exit code is
    STILL_ACTIVE); False for a dead or inaccessible process."""
    if sys.platform != "win32":
        raise OSError("process lookup requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def process_name(pid: int) -> str:
    """Executable file name (e.g. "chrome.exe") of a process, or "" when it cannot be opened."""
    if sys.platform != "win32":
        raise OSError("process lookup requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    query_limited_information = 0x1000
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


def wait_window_for_pid(
    pid: int, *, title_contains: str | None = None, timeout_s: float = 30.0
) -> WindowInfo:
    """First visible titled window owned by `pid`, polling until it appears (30 s: a cold Chrome
    on a CI runner took over 15 s, 2026-11-04)."""
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


@functools.cache
def _user32() -> ctypes.WinDLL:
    """user32 with explicit signatures for the per-window helpers (loaded once)."""
    if sys.platform != "win32":
        raise OSError("window helpers require Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for name in ("IsWindow", "IsWindowEnabled", "IsIconic", "GetWindowTextLengthW"):
        getattr(user32, name).argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowEnabled.restype = wintypes.BOOL
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    return user32


def is_window(hwnd: int) -> bool:
    return bool(_user32().IsWindow(hwnd))


def is_window_enabled(hwnd: int) -> bool:
    """False while a modal owned dialog disables its owner window."""
    return bool(_user32().IsWindowEnabled(hwnd))


def is_minimized(hwnd: int) -> bool:
    return bool(_user32().IsIconic(hwnd))


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_UOI_NAME = 2
_DESKTOP_READOBJECTS = 0x0001


def is_hung(hwnd: int) -> bool:
    """`IsHungAppWindow`: the window's thread has not pumped messages for about five seconds;
    input queued to it replays later against a different UI (PX-18)."""
    user32 = _user32()
    user32.IsHungAppWindow.argtypes = (wintypes.HWND,)
    user32.IsHungAppWindow.restype = wintypes.BOOL
    return bool(user32.IsHungAppWindow(hwnd))


def input_desktop_name() -> str:
    """Name of the desktop receiving input (`OpenInputDesktop` + `GetUserObjectInformation`):
    "Default" normally, "Winlogon" on the secure desktop (UAC, lock screen) where SendInput is
    dropped (PX-17)."""
    user32 = _user32()
    user32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.GetUserObjectInformationW.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetUserObjectInformationW.restype = wintypes.BOOL
    user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
    user32.CloseDesktop.restype = wintypes.BOOL
    desktop = user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not desktop:
        raise OSError(f"OpenInputDesktop failed: error {ctypes.get_last_error()}")
    try:
        buffer = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user32.GetUserObjectInformationW(
            desktop, _UOI_NAME, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
        ):
            raise OSError(f"GetUserObjectInformation failed: error {ctypes.get_last_error()}")
        return buffer.value
    finally:
        user32.CloseDesktop(desktop)


def window_title(hwnd: int) -> str:
    user32 = _user32()
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value
