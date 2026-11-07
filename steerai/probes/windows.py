"""Top-level window enumeration via the Win32 API (ctypes, no third-party dependency)."""

from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import sys
import time
from collections.abc import Callable, Mapping
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    minimized: bool = False
    """Minimized windows sit at (-32000, -32000) with an empty UIA rect; they are not drivable."""
    cloak_unreadable: bool = False
    """DWM did not answer whether it hides the window (issue #48): it counts in a before/after
    window list, so a diff never sees a change that did not happen, but never as a target."""


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
        if not user32.IsWindowVisible(hwnd):
            return True
        cloaked = is_cloaked(hwnd)
        if cloaked is True:
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
                cloak_unreadable=cloaked is None,
            )
        )
        return True

    user32.EnumWindows(enum_proc(on_window), 0)
    return found


def is_cloaked(hwnd: int) -> bool | None:
    """True for windows DWM hides although IsWindowVisible says visible (UWP hosts, other desktops).

    Such windows have a Win32 rect but an empty UIA tree and rect; they are never targets. None
    when DWM does not answer (any HRESULT but S_OK; measured E_HANDLE for a dead handle): unclear,
    never visible (issue #48).
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
    cloaked = ctypes.c_uint32(0)  # a DWORD is 32 bits; wintypes.DWORD is 8 bytes off Windows
    result = dwmapi.DwmGetWindowAttribute(
        hwnd, dwmwa_cloaked, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    if result != 0:
        return None
    return cloaked.value != 0


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
    with contextlib.suppress(OSError):
        # started from Explorer, the parent chain reaches the shell: the desktop and every
        # File Explorer window stay targetable (OW-18)
        pids.discard(desktop_window().pid)
    return hwnds, pids


_ERROR_INVALID_PARAMETER = 87
"""OpenProcess's error for a pid with no process (measured: dead pids and pid 0)."""


def process_alive(pid: int) -> bool:
    """Whether a process with this id is still running. False only when it is known gone: no
    such process (OpenProcess fails ERROR_INVALID_PARAMETER) or an exit code other than
    STILL_ACTIVE. An access-denied process exists (issue #69: 140 of 285 running processes
    refuse a query handle here), and any other failure is unknown: both count as running."""
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
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # unreadable: unknown, never "gone"
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


class Pollable(Protocol):
    """A launched process as `wait_window_for_pid` reports it (a `subprocess.Popen`)."""

    def poll(self) -> int | None: ...


def wait_window_for_pid(
    pid: int,
    *,
    title_contains: str | None = None,
    timeout_s: float = 30.0,
    image: str | None = None,
    process: Pollable | None = None,
) -> WindowInfo:
    """First visible titled window owned by `pid`, polling until it appears (30 s: a cold Chrome
    on a CI runner took over 15 s, 2026-11-04). On timeout the message says whether `process`
    is still running or exited, and with what code (issue #39). A process that has exited can
    no longer own a window: the wait ends at once (hunter CC-11)."""
    started = time.monotonic()
    deadline = started + timeout_s
    while True:
        for window in list_top_windows():
            if window.cloak_unreadable:
                continue  # unclear is never a match (issue #48)
            if window.pid == pid and (title_contains is None or title_contains in window.title):
                return window
        code = None if process is None else process.poll()
        if code is not None or time.monotonic() >= deadline:
            seen = _windows_of(image) if image is not None else ""
            state = ""
            if process is not None:
                state = "; process running" if code is None else f"; process {exit_state(code)}"
            waited = time.monotonic() - started
            raise TimeoutError(
                f"no visible window for pid {pid} within {timeout_s} s"
                f" (after {waited:.1f} s){state}{seen}"
            )
        time.sleep(0.2)


def exit_state(code: int) -> str:
    """An exit code as CI logs read it: a crash's NTSTATUS in hex too (hunter CC-31)."""
    unsigned = code & 0xFFFFFFFF
    return f"exit={code}" + (f" (0x{unsigned:08X})" if unsigned >= 0x80000000 else "")


def _windows_of(image: str) -> str:
    """The visible windows of `image` by pid and title length (never the title: it can name
    a person), so a launch handed to another process is told from no window at all."""
    found = [
        f"pid {w.pid} ({len(w.title)} chars{', minimized' if w.minimized else ''}"
        f"{', cloak unreadable' if w.cloak_unreadable else ''})"
        for w in list_top_windows()
        if process_name(w.pid).lower() == image.lower()
    ]
    return f"; {image} windows: " + (", ".join(found) if found else "none") + _hidden_of(image)


def _hidden_of(image: str) -> str:
    """Counts (never titles) of `image`'s top windows the list leaves out, so "no window" is
    told from "a hidden window" (issue #39, schema gate item 2)."""
    rows: list[tuple[bool, int, bool | None]] = []
    user32 = _user32()
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (enum_proc, wintypes.LPARAM)

    def on_window(hwnd: int, _lparam: int) -> bool:
        if process_name(window_pid(hwnd)).lower() == image.lower():
            rows.append(
                (
                    bool(user32.IsWindowVisible(hwnd)),
                    user32.GetWindowTextLengthW(hwnd),
                    is_cloaked(hwnd),
                )
            )
        return True

    user32.EnumWindows(enum_proc(on_window), 0)
    return hidden_counts(rows)


def hidden_counts(rows: list[tuple[bool, int, bool | None]]) -> str:
    """`(visible, title length, cloaked)` per top window, as counts of the kinds the visible
    list drops."""
    invisible = sum(1 for visible, _, _ in rows if not visible)
    untitled = sum(1 for visible, length, _ in rows if visible and length == 0)
    cloaked = sum(1 for visible, length, c in rows if visible and length and c is True)
    unclear = sum(1 for visible, length, c in rows if visible and length and c is None)
    return (
        f"; all top windows {len(rows)}: invisible {invisible}, untitled {untitled},"
        f" cloaked {cloaked}, cloak unreadable {unclear}"
    )


def desktop_window() -> WindowInfo:
    """The shell's desktop (`Progman`), the window a task starts on when the person names no
    app: its UIA tree lists the desktop icons; an `open` step leaves it for the app it launches."""
    user32 = _user32()
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = int(user32.FindWindowW("Progman", None) or 0)
    if not hwnd:
        raise OSError("the desktop window (Progman) was not found")
    return WindowInfo(hwnd=hwnd, title=window_title(hwnd), pid=window_pid(hwnd))


def window_identity(hwnd: int) -> dict[str, object] | None:
    """What a resume re-binds an opened window by: handle, pid, process start time, image name
    and the live title, plus whether it is a visible, top-level, minimized window; None when the
    handle is no longer a window (schema gate P7, step-window SW-12/14)."""
    if not is_window(hwnd):
        return None
    pid = window_pid(hwnd)
    user32 = _user32()
    return {
        "hwnd": hwnd,
        "pid": pid,
        "start": process_start_time(pid),
        "image": process_name(pid).lower(),
        "title": window_title(hwnd),
        "visible": bool(user32.IsWindowVisible(hwnd)),
        # a reused handle can come back as a child window of the same process (SW-12)
        "root": int(user32.GetAncestor(hwnd, _GA_ROOT) or 0) == hwnd,
        "minimized": bool(user32.IsIconic(hwnd)),
        "cloaked": is_cloaked(hwnd),  # DWMWA_CLOAKED: another virtual desktop (SF-10)
    }


def find_window(title_substring: str) -> WindowInfo | None:
    """First visible top-level window whose title contains the text, case-insensitive."""
    needle = title_substring.lower()
    for window in list_top_windows():
        if not window.cloak_unreadable and needle in window.title.lower():
            return window
    return None


_GA_ROOT = 2
"""`GetAncestor(GA_ROOT)`: the root window of a handle (winuser.h)."""


@functools.cache
def _user32() -> ctypes.WinDLL:
    """user32 with explicit signatures for the per-window helpers (loaded once)."""
    if sys.platform != "win32":
        raise OSError("window helpers require Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for name in (
        "IsWindow",
        "IsWindowEnabled",
        "IsIconic",
        "IsWindowVisible",
        "GetWindowTextLengthW",
    ):
        getattr(user32, name).argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
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


# ---- the session's lock state (issue #31) ------------------------------------------------------

_WTS_CURRENT_SESSION = 0xFFFFFFFF
_WTS_SESSION_INFO_EX = 25
"""`WTSSessionInfoEx`, index 25 of `WTS_INFO_CLASS` (learn.microsoft.com, wtsapi32.h)."""
_WTS_SESSIONSTATE_LOCK = 0
_WTS_SESSIONSTATE_UNLOCK = 1


class _WtsInfoExLevel1(ctypes.Structure):
    """`WTSINFOEX_LEVEL1_W` (learn.microsoft.com, wtsapi32.h); 232 bytes inside `WTSINFOEXW`,
    measured equal to the size the call returns (2026-11-06)."""

    _fields_ = (
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", ctypes.c_long),
        ("WinStationName", wintypes.WCHAR * 33),
        ("UserName", wintypes.WCHAR * 21),
        ("DomainName", wintypes.WCHAR * 18),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    )


class _WtsInfoExData(ctypes.Union):
    _fields_ = (("Level1", _WtsInfoExLevel1),)


class _WtsInfoEx(ctypes.Structure):
    _fields_ = (("Level", wintypes.DWORD), ("Data", _WtsInfoExData))


@functools.cache
def _wtsapi32() -> ctypes.WinDLL:
    """Loaded once, prototypes set once (read at every step boundary, p50 0.2 ms)."""
    dll = ctypes.WinDLL("wtsapi32", use_last_error=True)
    dll.WTSQuerySessionInformationW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    dll.WTSQuerySessionInformationW.restype = wintypes.BOOL
    dll.WTSFreeMemory.argtypes = (ctypes.c_void_p,)
    dll.WTSFreeMemory.restype = None
    return dll


_WTS_ACTIVE = 0
"""`WTSActive` in `WTS_CONNECTSTATE_CLASS`: a user is logged on and the session is attached
(measured 0 on this console, 2026-11-06)."""


def lock_state(*, session_state: int, session_flags: int) -> bool | None:
    """The lock flag of an active session: True locked, False unlocked, None for anything else
    (a disconnected RDP session, session 0, `WTS_SESSIONSTATE_UNKNOWN`). Python 3.12 runs on
    Windows 8.1 and later only (docs.python.org/3.12/using/windows.html), so the flags' Windows 7
    reversal never applies."""
    if session_state != _WTS_ACTIVE:
        return None
    if session_flags == _WTS_SESSIONSTATE_LOCK:
        return True
    if session_flags == _WTS_SESSIONSTATE_UNLOCK:
        return False
    return None


def session_locked() -> bool | None:
    """Whether this process's session is locked: True, False, or None when Windows does not say
    or the session is not active (a failed call too). The Windows 10 lock screen is
    `LockApp.exe` on the default desktop, so the input desktop's name does not show it (#31)."""
    wtsapi32 = _wtsapi32()
    buffer = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not wtsapi32.WTSQuerySessionInformationW(
        None, _WTS_CURRENT_SESSION, _WTS_SESSION_INFO_EX, ctypes.byref(buffer), ctypes.byref(size)
    ):
        return None
    try:
        if size.value < ctypes.sizeof(_WtsInfoEx):
            return None
        info = ctypes.cast(buffer, ctypes.POINTER(_WtsInfoEx)).contents
        if info.Level != 1:
            return None
        level1 = info.Data.Level1
        return lock_state(session_state=level1.SessionState, session_flags=level1.SessionFlags)
    finally:
        wtsapi32.WTSFreeMemory(buffer)
