# ruff: noqa: T201, E501
"""Diagnostic only (probe branch, never merged): what the real low-level hooks hand the
callback for a numbered probe. Prints; passes."""

from __future__ import annotations

import ctypes
import time
from typing import Any

import pytest

from steerai.probes import win_input
from steerai.session.hook_classifier import LLMHF_INJECTED, probe_tag
from steerai.session.monitor import HookMonitor

pytestmark = pytest.mark.windows

TAG = 0x1234_5678
VK = 0x0E


def test_capture_what_the_real_hooks_see(capfd: pytest.CaptureFixture[str]) -> None:
    rows: list[str] = []
    monitor = HookMonitor(per_hook=True, probe_vk=VK, extra_info=TAG)
    monitor.start()
    core = monitor._core
    cls = core._classifier
    real_kb, real_ms = core.on_keyboard, core.on_mouse

    def kb(*, flags: int, extra: int, now: float, vk: int = 0) -> None:
        rows.append(
            f"KB vk={vk:#x} flags={flags:#x} extra={extra:#018x} "
            f"own_seq(probe_shaped={vk == VK and bool(flags & 0x80)},"
            f"injected={bool(flags & 0x10)})="
            f"{cls._own_seq(extra, probe_shaped=vk == VK and bool(flags & 0x80), injected=bool(flags & 0x10))}"
        )
        real_kb(flags=flags, extra=extra, now=now, vk=vk)

    def ms(*, message: int, flags: int, extra: int, pt: tuple[int, int], now: float) -> None:
        rows.append(
            f"MS msg={message:#x} flags={flags:#x} extra={extra:#018x} pt={pt} own_seq="
            f"{cls._own_seq(extra, probe_shaped=message == 0x200, injected=bool(flags & 1))}"
        )
        real_ms(message=message, flags=flags, extra=extra, pt=pt, now=now)

    core.on_keyboard = kb  # type: ignore[method-assign]
    core.on_mouse = ms  # type: ignore[method-assign]

    def step(label: str, send: Any) -> None:
        rows.append(f"--- {label}")
        try:
            send()
        except Exception as exc:  # noqa: BLE001 - diagnostic: record any failure
            rows.append(f"SEND RAISED {exc!r}")
        time.sleep(0.4)

    try:
        user32 = win_input._user32()
        step(
            "numbered key-up (shipped key_up_probe seq=7)",
            lambda: win_input.key_up_probe(VK, tag=TAG, seq=7),
        )
        step(
            "numbered nudge (shipped nudge_in_place seq=7)",
            lambda: win_input.nudge_in_place(tag=TAG, seq=7),
        )
        x, y = win_input.cursor_pos()
        ax, ay = win_input._to_absolute(user32, x, y)
        flags = (
            win_input._MOUSEEVENTF_MOVE
            | win_input._MOUSEEVENTF_ABSOLUTE
            | win_input._MOUSEEVENTF_VIRTUALDESK
        )
        step(
            "numbered nudge WITHOUT nocoalesce (raw)",
            lambda: win_input._send(
                user32,
                win_input.build_mouse_input(dx=ax, dy=ay, flags=flags, tag=probe_tag(TAG, 7)),
            ),
        )
        step("bare-tag nudge (v1 shape)", lambda: win_input.nudge_in_place(tag=TAG))
        step("bare-tag key-up", lambda: win_input.key_up_probe(VK, tag=TAG))
        step("monitor.probe_now()", monitor.probe_now)
        rows.append(
            f"probe_seen={monitor.probe_seen()} healthy={monitor.healthy()} "
            f"lost={monitor.lost_hooks()} seq={core._ledger._seq:#x}"
        )
    finally:
        monitor.close()
    rows.append(
        f"sizeof(ULONG_PTR)={ctypes.sizeof(win_input.ULONG_PTR)} "
        f"sizeof(INPUT)={ctypes.sizeof(win_input.INPUT)} LLMHF_INJECTED={LLMHF_INJECTED:#x}"
    )
    with capfd.disabled():
        print("\nPROBE_CAPTURE_BEGIN")
        for row in rows:
            print(row)
        print("PROBE_CAPTURE_END", flush=True)
