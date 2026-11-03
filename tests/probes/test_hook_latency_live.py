"""Platform facts the takeover design rests on (probe 2026-11-04): what a low-level hook sees."""

from __future__ import annotations

import ctypes
import time

import pytest

from steerai.probes.hook_latency import MOUSEEVENTF_MOVE, HookThread, Ledger, inject_probe
from steerai.probes.win_input import _user32, build_mouse_input, cursor_pos

pytestmark = [pytest.mark.windows, pytest.mark.probe]


def _with_hooks(send: object, repeats: int = 5) -> Ledger:
    ledger = Ledger()
    hooks = HookThread(ledger)
    hooks.start()
    try:
        for _ in range(repeats):
            ledger.sent(time.perf_counter_ns())
            assert callable(send)
            send()
            time.sleep(0.05)
        time.sleep(0.3)
    finally:
        hooks.stop()
    return ledger


def test_an_absolute_move_to_the_current_position_reaches_the_hook_without_moving() -> None:
    before = cursor_pos()
    ledger = _with_hooks(inject_probe)
    assert ledger.counts.get("steer", 0) >= 5
    assert len(ledger.samples) == 5
    assert ledger.unmatched == 0
    assert cursor_pos() == before
    assert max(s.latency_ns for s in ledger.samples) < 1_000_000_000, "under the 1 s ceiling"


def test_a_zero_delta_relative_move_never_reaches_the_hook() -> None:
    user32 = _user32()

    def send() -> None:
        record = build_mouse_input(dx=0, dy=0, flags=MOUSEEVENTF_MOVE)
        assert user32.SendInput(1, ctypes.byref(record), ctypes.sizeof(record)) == 1

    ledger = _with_hooks(send)
    assert ledger.counts.get("steer", 0) == 0
    assert ledger.samples == []
