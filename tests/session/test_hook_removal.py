"""#364 P3 ([V2.57] LW-01, [V2.59]): a hook Windows removes, and a callback slower than the
derived deadline, both end in monitor_lost with nothing sent.

Measured on this machine (2026-11-15, scratch probe `hook_removal_probe.py`, real hooks, tagged
key-ups into our own Notepad): after 1, 2, 3, 5 and 8 consecutive keyboard callbacks stalled
2.0 s each, Windows did NOT remove the hook: 20 of 20 later probes were delivered every time, and
every stalled callback still counted, late. Microsoft Learn's wording ("the hook is silently
removed") gives no count. So removal is modelled with a real unhook of one hook
(`unhook_for_test("keyboard")`), and the stall case is its own test: the hook survives, but an
echo slower than the deadline is lost (fail closed).

Windows only, on CI's windows-latest or a cleared machine: real hooks, a mouse nudge in place and
key-ups of the unassigned probe key."""

from __future__ import annotations

import time

import pytest

from steerai.loop2.loop import PortInterrupted
from steerai.session.monitor import PROBE_VK, HookMonitor
from tests.loop2.test_live_world import _RealTag, _world

pytestmark = pytest.mark.windows

STALL_S = 2.0
"""Past the 1000 ms cap whatever LowLevelHooksTimeout says."""


class _RealMonitor(_RealTag):
    """The fake platform, but the monitor is the real per-hook one on the run's tag."""

    def start_monitor(  # type: ignore[override]  # the real monitor in the fake's place
        self, kill_key: str, _clock: object, extra_info: int, deadline_s: float
    ) -> HookMonitor:
        self.calls.append(f"monitor:{kill_key}")
        self.real = HookMonitor(
            kill_key="ctrl+alt+shift+f9",
            extra_info=extra_info,
            per_hook=True,
            probe_vk=PROBE_VK,
            probe_deadline_s=deadline_s,
        )
        self.deadline_s = deadline_s
        self.real.start()
        return self.real


def test_a_keyboard_hook_removed_by_the_os_is_lost_and_nothing_is_sent() -> None:
    platform = _RealMonitor()
    world, _ = _world(platform, clock=time.monotonic, pause=time.sleep)
    try:
        world.desk.prove_hooks()  # both hooks alive: proven
        platform.real.unhook_for_test("keyboard")  # removed, as Windows removes a hook
        with pytest.raises(PortInterrupted) as lost:
            world.desk.prove_hooks()
        assert [lost.value.token] == ["monitor_lost"]
        time.sleep(platform.deadline_s)
        assert platform.real.lost_hooks() == ("keyboard",)
        assert platform.fake_input.bursts == []
        assert platform.fake_input.clicks == []
        assert world.report()["monitor_lost"] == 1
    finally:
        world.close()


def test_an_echo_slower_than_the_deadline_is_lost_though_windows_keeps_the_hook() -> None:
    """[V2.59]: the stalled callback runs late and Windows keeps the hook, but the proof
    waits only the derived deadline: monitor_lost, fail closed, nothing sent."""
    platform = _RealMonitor()
    world, _ = _world(platform, clock=time.monotonic, pause=time.sleep)
    try:
        world.desk.prove_hooks()
        platform.real.stall_next_for_test("keyboard", STALL_S)  # the next probe's echo hangs
        with pytest.raises(PortInterrupted) as lost:
            world.desk.prove_hooks()
        assert [lost.value.token] == ["monitor_lost"]
        assert platform.fake_input.bursts == []
        assert platform.fake_input.clicks == []
        time.sleep(STALL_S)  # the late echo lands; the loss stays latched
        assert world.interrupted() == "monitor_lost"
    finally:
        world.close()
