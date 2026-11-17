# ruff: noqa: T201, E501
"""Diagnostic only (probe branch, never merged): what the real low-level hooks hand the
callback for a probe carrying its own random 32-bit tag ([V2.81]), on both hooks. Prints the
raw values and passes. Run on a laptop to record the Windows build's behaviour."""

from __future__ import annotations

import platform
import sys
import time
from typing import Any

import pytest

from steerai.probes import win_input
from steerai.session.monitor import HookMonitor

pytestmark = pytest.mark.windows

TAG = 0x1234_5678
VK = 0x0E


def test_capture_what_the_real_hooks_see(capfd: pytest.CaptureFixture[str]) -> None:
    rows: list[str] = [f"windows={sys.getwindowsversion()} platform={platform.platform()}"]
    monitor = HookMonitor(per_hook=True, probe_vk=VK, extra_info=TAG)
    monitor.start()
    core = monitor._core
    cls = core._classifier
    real_kb, real_ms = core.on_keyboard, core.on_mouse

    def kb(*, flags: int, extra: int, now: float, vk: int = 0) -> None:
        shaped = vk == VK and bool(flags & 0x80)
        rows.append(
            f"KB vk={vk:#x} flags={flags:#x} extra={extra:#018x} high32={extra >> 32:#x} "
            f"outstanding={cls._probe_tag:#x} own={cls._own(extra, probe_shaped=shaped, injected=bool(flags & 0x10))}"
        )
        real_kb(flags=flags, extra=extra, now=now, vk=vk)

    def ms(*, message: int, flags: int, extra: int, pt: tuple[int, int], now: float) -> None:
        rows.append(
            f"MS msg={message:#x} flags={flags:#x} extra={extra:#018x} high32={extra >> 32:#x} pt={pt} "
            f"outstanding={cls._probe_tag:#x} own={cls._own(extra, probe_shaped=message == 0x200, injected=bool(flags & 1))}"
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
        for n in range(1, 4):
            step(f"monitor.probe_now() #{n} (a fresh random tag on both hooks)", monitor.probe_now)
            rows.append(
                f"  tag={core._ledger._tag:#x} proof(mouse)={cls.proof('mouse'):#x} "
                f"proof(keyboard)={cls.proof('keyboard'):#x} probe_seen={monitor.probe_seen()} "
                f"lost={monitor.lost_hooks()}"
            )
        high = iter((0x8000_0001, 0xC0DE_BEEF, 0xFFFF_FFFE))
        core._tags._rand32 = lambda: next(high)  # type: ignore[attr-defined]  # tags with bit 31 set
        for n in range(1, 4):
            step(f"monitor.probe_now() high-bit tag #{n}", monitor.probe_now)
            rows.append(
                f"  tag={core._ledger._tag:#x} proof(mouse)={cls.proof('mouse'):#x} "
                f"proof(keyboard)={cls.proof('keyboard'):#x} probe_seen={monitor.probe_seen()}"
            )
            for hook in ("mouse", "keyboard"):
                got = cls.proof(hook)
                kind = (
                    "proven" if got == core._ledger._tag else "NOT proven (zero- or sign-extended?)"
                )
                rows.append(f"  {hook}: {kind}")
                step(
                    "an action-shaped nudge with the run tag",
                    lambda: win_input.nudge_in_place(tag=TAG),
                )
        step(
            "an action-shaped key-up with the run tag", lambda: win_input.key_up_probe(VK, tag=TAG)
        )
        rows.append(f"after actions: probe_seen={monitor.probe_seen()} healthy={monitor.healthy()}")
    finally:
        monitor.close()
    with capfd.disabled():
        print("\nPROBE_CAPTURE_BEGIN")
        for row in rows:
            print(row)
        print("PROBE_CAPTURE_END", flush=True)
