"""A live run that survives low memory ([V2.14] decision 4): a pre-flight refusal below a free
physical memory floor, before each task or chunk, instead of being stopped mid-run; and one
browser at a time in a process.

Free physical memory is `MEMORYSTATUSEX.ullAvailPhys` from `GlobalMemoryStatusEx` (kernel32;
Microsoft Learn, "GlobalMemoryStatusEx function (sysinfoapi.h)" and "MEMORYSTATUSEX structure
(sysinfoapi.h)": `dwLength` must be set to the structure's size before the call; `ullAvailPhys`
is the physical memory that can be reused at once without a write to disk, the sum of the
standby, free and zero lists). No new dependency.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

FLOOR_MB = 4096
"""UNMEASURED guard value. No probe has measured a Chrome working set or the free memory at which
the OS stopped a run. The machine showed 3051 MB free of 16201 MB (2026-11-14) after three
measurement runs had been stopped for low memory, so the floor sits above that reading. Override
with `--min-free-mb`; replace with N x a measured per-Chrome working set once one is recorded."""
EXIT_LOW_MEMORY = 75
"""The refusal's exit code: distinct from pass (0), fail (1) and usage (2); sysexits'
EX_TEMPFAIL, a condition that may pass if the run is tried again later."""
_MB = 1024 * 1024


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [  # the MEMORYSTATUSEX layout, in order
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def free_physical_mb() -> int:
    """Free physical memory in MB; OSError when it cannot be read (or off Windows)."""
    if sys.platform != "win32":
        raise OSError("GlobalMemoryStatusEx is Windows only")
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.ullAvailPhys // _MB)


class LowMemory(Exception):  # noqa: N818 - a run refusal, named for what the person sees
    """The run refused to start a task or chunk: free memory below the floor, or unreadable."""

    def __init__(self, free_mb: int | None, floor_mb: int) -> None:
        self.free_mb = free_mb
        self.floor_mb = floor_mb
        seen = "free memory is unreadable" if free_mb is None else f"{free_mb} MB free"
        super().__init__(
            f"refused to start: {seen}, below the floor {floor_mb} MB (--min-free-mb). "
            "Close other apps or run on an idle machine; finished rows are kept."
        )


def preflight(*, floor_mb: int, read: Callable[[], int] = free_physical_mb) -> int:
    """The free MB when at or above `floor_mb`; else LowMemory. An unreadable reading refuses
    (unclear means fail). A floor of 0 never refuses a reading."""
    try:
        free_mb = read()
    except OSError as exc:
        raise LowMemory(None, floor_mb) from exc
    if free_mb < floor_mb:
        raise LowMemory(free_mb, floor_mb)
    return free_mb


class BrowserBusyError(RuntimeError):
    """A second browser was asked for while one is open in this process."""


_SLOT = threading.Lock()


def claim_browser() -> None:
    """Take the process's one browser slot, or BrowserBusyError; pair with `release_browser`."""
    if not _SLOT.acquire(blocking=False):
        raise BrowserBusyError("one_browser")


def release_browser() -> None:
    _SLOT.release()


@contextmanager
def one_browser() -> Iterator[None]:
    """Hold the one browser slot for the body, released however it ends."""
    claim_browser()
    try:
        yield
    finally:
        release_browser()


def refusal_row(refused: LowMemory, remaining: list[str]) -> dict[str, Any]:  # Any: JSON values
    """The run's last row when the pre-flight refused: what was free, the floor, and what was not
    run (`task:run`), so the person can resume with `--tasks`."""
    return {
        "refused": "low_memory",
        "free_mb": refused.free_mb,
        "floor_mb": refused.floor_mb,
        "remaining": remaining,
    }


def guarded_launch[T](
    launch: Callable[..., T],
    *args: Any,  # Any: the launcher's own positional arguments
    floor_mb: int,
    read: Callable[[], int] = free_physical_mb,
    **kwargs: Any,  # Any: the launcher's own keyword arguments
) -> T:
    """`launch(*args, **kwargs)` after the pre-flight, holding the one browser slot until
    `guarded_stop`; a failed launch frees it at once."""
    preflight(floor_mb=floor_mb, read=read)
    claim_browser()
    try:
        return launch(*args, **kwargs)
    except BaseException:
        release_browser()
        raise


def guarded_stop[P](stop: Callable[[P, Path], None], proc: P, profile: Path) -> None:
    """`stop(proc, profile)` for the browser `guarded_launch` started, then free the slot."""
    try:
        stop(proc, profile)
    finally:
        release_browser()
