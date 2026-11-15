"""#364 P3 ([V2.57] LW-01): the OS silently removes a hook whose callback outlives
LowLevelHooksTimeout (Microsoft Learn, LowLevelKeyboardProc: "on Windows 7 and later, the hook
is silently removed without being called"; at most 1000 ms on Windows 10 1709 and later). The
v2 world must then lose the monitor, name the hook, and send nothing.

Windows only, on CI's windows-latest: it installs real hooks and sends tagged probes (a mouse
nudge in place and key-ups of the unassigned probe key). Never run locally."""

from __future__ import annotations

import time

import pytest

from steerai.loop2.loop import PortInterrupted
from steerai.session.monitor import PROBE_DEADLINE_S, PROBE_VK, HookMonitor
from tests.loop2.test_live_world import _RealTag, _world

pytestmark = pytest.mark.windows

STALL_S = 2.0
"""Past the 1000 ms cap whatever LowLevelHooksTimeout says."""


class _RealMonitor(_RealTag):
    """The fake platform, but the monitor is the real per-hook one on the run's tag."""

    def start_monitor(  # type: ignore[override]  # the real monitor in the fake's place
        self, kill_key: str, _clock: object, extra_info: int
    ) -> HookMonitor:
        self.calls.append(f"monitor:{kill_key}")
        self.real = HookMonitor(
            kill_key="ctrl+alt+shift+f9", extra_info=extra_info, per_hook=True, probe_vk=PROBE_VK
        )
        self.real.start()
        return self.real


def test_a_keyboard_hook_removed_by_the_os_is_lost_and_nothing_is_sent() -> None:
    platform = _RealMonitor()
    world, _ = _world(platform, clock=time.monotonic, pause=time.sleep)
    try:
        world.desk.prove_hooks()  # both hooks alive: proven
        platform.real.stall_next_for_test("keyboard", STALL_S)
        platform.real.probe_now()  # its key-up hangs the keyboard callback past the timeout
        time.sleep(STALL_S + 0.5)
        with pytest.raises(PortInterrupted) as lost:
            world.desk.prove_hooks()
        assert [lost.value.token] == ["monitor_lost"]
        time.sleep(PROBE_DEADLINE_S)
        assert platform.real.lost_hooks() == ("keyboard",)
        assert platform.fake_input.bursts == []
        assert platform.fake_input.clicks == []
        assert world.report()["monitor_lost"] == 1
    finally:
        world.close()
