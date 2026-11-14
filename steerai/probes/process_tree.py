"""Memory of a process tree, for the working-set probe ([V2.19] item 3, [V2.14] 4).

Pure part: the tree of a root pid over (pid, parent pid) pairs, and the summary of samples.
Live part (Windows, ctypes, no new dependency):

- the process table from `CreateToolhelp32Snapshot` with `TH32CS_SNAPPROCESS`, walked with
  `Process32FirstW` / `Process32NextW`; `PROCESSENTRY32W.th32ParentProcessID` is the parent link
  (Microsoft Learn, "CreateToolhelp32Snapshot function (tlhelp32.h)", "Process32First function",
  "PROCESSENTRY32 structure": `dwSize` must be set before the first call);
- the working set from `K32GetProcessMemoryInfo` into `PROCESS_MEMORY_COUNTERS` (Microsoft Learn,
  "GetProcessMemoryInfo function (psapi.h)", "PROCESS_MEMORY_COUNTERS structure (psapi.h)":
  `WorkingSetSize` and `PeakWorkingSetSize` are in bytes, `cb` is the structure size).

A parent link can name a pid that was reused, so a tree is a best-effort view of one instant.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

_MB = 1024 * 1024
TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
"""Unmeasured: whether this access right is enough for K32GetProcessMemoryInfo on every Chrome
process; a process that cannot be read is skipped and the sample's process count shows it."""


@dataclass(frozen=True, slots=True)
class Sample:
    """One reading: free physical memory and the summed working sets of a process tree."""

    at_s: float
    free_mb: int
    tree_ws_mb: float
    tree_peak_ws_mb: float
    processes: int


def tree_pids(root: int, table: Iterable[tuple[int, int]]) -> set[int]:
    """`root` and every descendant in `table` ((pid, parent pid) pairs); empty when `root` is not
    in the table (it has exited). A cycle from a reused pid ends the walk."""
    pairs = list(table)
    if all(pid != root for pid, _ in pairs):
        return set()
    found = {root}
    grew = True
    while grew:
        grew = False
        for pid, parent in pairs:
            if parent in found and pid not in found:
                found.add(pid)
                grew = True
    return found


def summarize(samples: Sequence[Sample]) -> dict[str, Any]:  # Any: JSON values
    """The report row: the highest summed working set seen, the highest sum of the per-process
    peaks (an upper bound: peaks need not coincide), the lowest free memory and the most
    processes. No samples: a row that says so."""
    if not samples:
        return {"samples": 0}
    return {
        "samples": len(samples),
        "peak_tree_ws_mb": round(max(s.tree_ws_mb for s in samples), 1),
        "peak_tree_sum_of_peaks_mb": round(max(s.tree_peak_ws_mb for s in samples), 1),
        "min_free_mb": min(s.free_mb for s in samples),
        "max_processes": max(s.processes for s in samples),
    }


class _ProcessEntry(ctypes.Structure):
    _fields_ = [  # PROCESSENTRY32W, in order
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _MemoryCounters(ctypes.Structure):
    _fields_ = [  # PROCESS_MEMORY_COUNTERS, in order
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_table() -> list[tuple[int, int]]:  # pragma: no cover - live
    """Every (pid, parent pid) now; OSError when the snapshot cannot be taken."""
    if sys.platform != "win32":
        raise OSError("Toolhelp32 is Windows only")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == wintypes.HANDLE(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    table: list[tuple[int, int]] = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return table


def working_set_mb(pid: int) -> tuple[float, float] | None:  # pragma: no cover - live
    """(WorkingSetSize, PeakWorkingSetSize) of `pid` in MB, or None when it cannot be opened or
    read (it exited, or access is denied: skipped, never guessed)."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.K32GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_MemoryCounters),
        wintypes.DWORD,
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(_MemoryCounters)
        if not kernel.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize / _MB, counters.PeakWorkingSetSize / _MB
    finally:
        kernel.CloseHandle(handle)


def read_tree(root: int) -> tuple[float, float, int]:  # pragma: no cover - live
    """(summed working set MB, summed peak working set MB, processes read) of `root`'s tree."""
    total = peak = 0.0
    count = 0
    for pid in tree_pids(root, process_table()):
        reading = working_set_mb(pid)
        if reading is None:
            continue
        total += reading[0]
        peak += reading[1]
        count += 1
    return total, peak, count
