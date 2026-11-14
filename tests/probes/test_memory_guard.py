"""[V2.14] 4: the live suite's pre-flight memory floor and the one-browser slot, without a live
run."""

from __future__ import annotations

import ctypes
import sys

import pytest

from steerai.probes import memory_guard
from steerai.probes.memory_guard import (
    EXIT_LOW_MEMORY,
    FLOOR_MB,
    BrowserBusyError,
    LowMemory,
    claim_browser,
    one_browser,
    preflight,
    release_browser,
)


def test_the_struct_matches_memorystatusex() -> None:
    """MEMORYSTATUSEX: two DWORDs and seven DWORDLONGs, 64 bytes."""
    assert ctypes.sizeof(memory_guard._MemoryStatusEx) == 64


def test_enough_free_memory_passes_and_reports_it() -> None:
    assert preflight(floor_mb=4096, read=lambda: 6000) == 6000


def test_below_the_floor_refuses_with_a_clear_message() -> None:
    with pytest.raises(LowMemory) as refused:
        preflight(floor_mb=4096, read=lambda: 2900)
    message = str(refused.value)
    assert "2900 MB free" in message
    assert "floor 4096 MB" in message
    assert "--min-free-mb" in message
    assert (refused.value.free_mb, refused.value.floor_mb) == (2900, 4096)


def test_exactly_the_floor_passes() -> None:
    assert preflight(floor_mb=4096, read=lambda: 4096) == 4096


def test_an_unreadable_reading_refuses_never_runs() -> None:
    def broken() -> int:
        raise OSError("GlobalMemoryStatusEx failed")

    with pytest.raises(LowMemory) as refused:
        preflight(floor_mb=4096, read=broken)
    assert refused.value.free_mb is None
    assert "unreadable" in str(refused.value)


def test_a_zero_floor_turns_the_check_off_but_still_reads() -> None:
    assert preflight(floor_mb=0, read=lambda: 10) == 10


def test_the_exit_code_is_distinct_from_pass_fail_and_usage() -> None:
    assert EXIT_LOW_MEMORY not in {0, 1, 2}
    assert FLOOR_MB > 0


def test_one_browser_at_a_time_refuses_a_second_claim() -> None:
    with one_browser(), pytest.raises(BrowserBusyError), one_browser():
        pass
    with one_browser():
        pass  # released after the first block


def test_claim_and_release_pair_for_a_long_lived_browser() -> None:
    claim_browser()
    try:
        with pytest.raises(BrowserBusyError):
            claim_browser()
    finally:
        release_browser()
    claim_browser()
    release_browser()


def test_the_slot_is_released_when_the_body_raises() -> None:
    with pytest.raises(KeyboardInterrupt), one_browser():
        raise KeyboardInterrupt
    with one_browser():
        pass


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="GlobalMemoryStatusEx is Windows only")
def test_the_real_reading_is_a_positive_number_of_megabytes() -> None:
    assert memory_guard.free_physical_mb() > 0
