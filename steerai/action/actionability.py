"""Actionability before physical input (design 5.9 [v0.7]).

A target must be visible, have area, be enabled, be stable (two bounding-rect samples at least
34 ms apart within half a pixel) and receive events (the window under its centre belongs to the
target process). Elevation is decided by comparing process integrity levels, never by SendInput's
return value; an unreadable target level means best effort, judged by the readback afterwards.
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

from steerai.errors import ErrorCode, SteerError
from steerai.world.elements import Box, Element

STABLE_GAP_S = 0.034
STABLE_TOLERANCE_PX = 0.5
MEDIUM_INTEGRITY = 0x2000


@dataclass(frozen=True, slots=True)
class Actionability:
    centre: tuple[int, int]
    box: Box


def rects_stable(a: Box, b: Box, *, tolerance: float = STABLE_TOLERANCE_PX) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def centre_of(box: Box) -> tuple[int, int]:
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


def check_actionable(
    element: Element, *, owner_pid_at_centre: int, target_pid: int
) -> Actionability:
    """Pure part of the check: geometry, state and the hit-test result the caller measured."""
    if element.offscreen:
        raise SteerError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"element {element.id} is off-screen")
    if not element.has_area:
        raise SteerError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"element {element.id} has no area")
    if not element.enabled:
        raise SteerError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"element {element.id} is disabled")
    if owner_pid_at_centre != target_pid:
        raise SteerError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"element {element.id} is covered: pid {owner_pid_at_centre} owns its centre",
        )
    return Actionability(centre=centre_of(element.box), box=element.box)


def integrity_allows_input(*, caller_level: int, target_level: int | None) -> bool:
    """UIPI: input from a lower integrity process into a higher one is silently dropped."""
    if target_level is None:
        return True
    return caller_level >= target_level


def sample_stable_rect(read_rect: Callable[[], Box], *, gap_s: float = STABLE_GAP_S) -> Box:
    """Two samples `gap_s` apart must agree within the tolerance, else `unstable_ui`."""
    first = read_rect()
    time.sleep(gap_s)
    second = read_rect()
    if not rects_stable(first, second):
        raise SteerError(
            ErrorCode.UNSTABLE_UI, f"element moved between samples: {first} -> {second}"
        )
    return second


def process_integrity_level(pid: int) -> int | None:
    """Mandatory integrity level of a process (0x1000 low, 0x2000 medium, 0x3000 high), or None."""
    if sys.platform != "win32":
        raise OSError("integrity levels require Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

    process_query_limited_information = 0x1000
    token_query = 0x0008
    token_integrity_level = 25
    process = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not process:
        return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(process, token_query, ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, token_integrity_level, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, token_integrity_level, buffer, size.value, ctypes.byref(size)
            ):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            count = advapi32.GetSidSubAuthorityCount(sid)[0]
            return int(advapi32.GetSidSubAuthority(sid, count - 1)[0])
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)
