"""[V2.60] hook helper, PR 4: `LiveWorld` over `HelperMonitor`, behind `LiveConfig.hook_helper`.

With the flag off nothing changes (the in-process `HookMonitor`). With it on, the platform starts
the helper monitor in the same place in the start order (after the session and the run's tag);
the world reads its session and desktop transitions as stops (§5.6), and its kill-aware sleeps
wait on the helper (wake or exit, HH-08) instead of a plain sleep. Fakes only."""

from __future__ import annotations

import dataclasses
import math
import time
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2 import live_world
from steerai.loop2.act_port import ActPort
from steerai.loop2.live_world import LiveWorld
from steerai.loop2.loop import PortInterrupted
from steerai.loop2.world_ports import KILL_POLL_S, kill_sleep
from tests.loop2.test_live_world import Clock, Monitor, Platform, _config, _world
from tests.loop2.world_fakes import task
from tests.session.test_helper_monitor import DEADLINE_S, _rig


class HelperMonitor(Monitor):
    """The fake helper monitor: a `Monitor` plus the session transitions and the helper wait."""

    def __init__(self, clock: Any) -> None:  # Any: the test clock
        super().__init__()
        self.change: str | None = None
        self.waits: list[float] = []
        self.clock = clock
        self.kill_after_waits: int | None = None
        self.returns_at_once = False
        """The helper exited, or its event is stuck: each wait returns at once."""
        self.kill_raises: Exception | None = None
        self.kill_reads = 0
        self.kill_raises_from = 1
        """The kill read (1-based) from which `kill_raises` is raised."""
        self.close_raises: Exception | None = None

    def close(self) -> None:
        super().close()
        if self.close_raises is not None:
            raise self.close_raises

    def kill_requested(self) -> bool:
        self.kill_reads += 1
        if self.kill_raises is not None and self.kill_reads >= self.kill_raises_from:
            raise self.kill_raises
        return super().kill_requested()

    def session_change(self) -> str | None:
        return self.change

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        if len(self.waits) > 1000:
            raise AssertionError("the sleep spun on the helper's wait")
        if not self.returns_at_once:
            self.clock.pause(seconds)
        if self.kill_after_waits is not None and len(self.waits) >= self.kill_after_waits:
            self.killed = True


class HelperPlatform(Platform):
    def __init__(self) -> None:
        super().__init__()
        self.helper_error: Exception | None = None
        self.helper: HelperMonitor | None = None

    def start_helper_monitor(
        self,
        kill_key: str,
        clock: Any,  # Any: the world's clock
        extra_info: int,
        deadline_s: float,
        hook_timeout_ms: int | None,
    ) -> HelperMonitor:
        self.calls.append(f"helper:{kill_key}")
        self.helper_args = (extra_info, deadline_s, hook_timeout_ms)
        if self.helper_error is not None:
            raise self.helper_error
        self.helper = HelperMonitor(clock)
        return self.helper


def test_with_the_flag_off_the_in_process_monitor_starts() -> None:
    _, platform = _world(HelperPlatform())
    assert "monitor:ctrl+alt+k" in platform.calls
    assert not [c for c in platform.calls if c.startswith("helper:")]


def test_with_the_flag_on_the_helper_monitor_starts_after_the_session_and_the_tag() -> None:
    platform = HelperPlatform()
    platform.hook_timeout = 700
    _world(platform, hook_helper=True)
    calls = platform.calls
    assert "helper:ctrl+alt+k" in calls
    assert not [c for c in calls if c.startswith("monitor:")]
    assert calls.index("session") < calls.index("tag") < calls.index("helper:ctrl+alt+k")
    tag, deadline, timeout = platform.helper_args
    assert tag == platform.input_tag
    assert deadline == pytest.approx(0.7 + 0.05)  # [V2.59]: the timeout read at start + margin
    assert timeout == 700


def test_a_helper_refusal_is_raised_as_it_is_and_gives_everything_back() -> None:
    """K16: if the helper fails to start, v2 refuses to start."""
    platform = HelperPlatform()
    platform.helper_error = SteerError(ErrorCode.GATE_BLOCKED, "kill_key_unavailable: held")
    with pytest.raises(SteerError, match="kill_key_unavailable"):
        _world(platform, hook_helper=True)
    assert "untag" in platform.calls
    assert "release_session" in platform.calls


def test_any_other_helper_start_failure_is_monitor_unavailable() -> None:
    platform = HelperPlatform()
    platform.helper_error = RuntimeError("an unexpected failure")
    with pytest.raises(SteerError) as refused:
        _world(platform, hook_helper=True)
    assert refused.value.code is ErrorCode.GATE_BLOCKED
    assert refused.value.message == "monitor_unavailable"
    assert "release_session" in platform.calls


@pytest.mark.parametrize("token", ["session_locked", "secure_desktop"])
def test_a_session_or_desktop_transition_is_a_latched_stop(token: str) -> None:
    """§5.6: a transition seen by the helper's watchdog stops the task, and stays stopped."""
    platform = HelperPlatform()
    world, _ = _world(platform, hook_helper=True)
    assert world.interrupted() is None
    assert platform.helper is not None
    platform.helper.change = token
    assert world.interrupted() == token
    platform.helper.change = None
    assert world.interrupted() == token


def test_kill_aware_sleeps_wait_on_the_helper_for_the_whole_time() -> None:
    """HH-08: the world's sleeps wait on the helper (wake or exit) in steps, re-reading the
    switch between them, and last as long as asked."""
    platform = HelperPlatform()
    _world(platform, hook_helper=True)
    helper = platform.helper
    assert helper is not None
    sleep = platform.observer_sleep
    start = helper.clock()
    sleep(0.12)
    assert helper.clock() - start == pytest.approx(0.12)
    assert all(w <= KILL_POLL_S + 1e-9 for w in helper.waits)
    assert sum(helper.waits) == pytest.approx(0.12)


def test_a_kill_during_a_helper_wait_ends_the_sleep() -> None:
    platform = HelperPlatform()
    _world(platform, hook_helper=True)
    helper = platform.helper
    assert helper is not None
    helper.kill_after_waits = 1
    with pytest.raises(PortInterrupted) as stopped:
        platform.observer_sleep(10.0)
    assert stopped.value.token == "killed"  # noqa: S105 - a stop token, not a password
    assert len(helper.waits) == 1


def test_with_the_flag_off_sleeps_are_plain() -> None:
    platform = HelperPlatform()
    _world(platform)
    assert platform.helper is None
    platform.observer_sleep(0.1)  # the plain kill-aware sleep on the world's pause


def test_the_header_names_which_monitor_runs() -> None:
    from tests.loop2.world_fakes import task  # noqa: PLC0415 - this test only

    helper_world, _ = _world(HelperPlatform(), hook_helper=True)
    plain_world, _ = _world(HelperPlatform())
    assert helper_world.header(task())["hook_monitor"] == "helper"
    assert plain_world.header(task())["hook_monitor"] == "in_process"


def test_a_kill_read_that_raises_is_a_lost_monitor_never_an_exception() -> None:
    """#383 principal note 2: a closed helper's kill word cannot be read, and `interrupted`
    never raises; the stop is latched and counted once."""
    platform = HelperPlatform()
    world, _ = _world(platform, hook_helper=True)
    assert platform.helper is not None
    platform.helper.kill_raises = RuntimeError("the helper is not started")
    assert world.interrupted() == "monitor_lost"
    assert world.interrupted() == "monitor_lost"
    assert world.monitor_lost == 1


def test_the_hook_timeout_is_read_once_at_start_and_that_value_passed_on() -> None:
    """[V2.59], #385 principal 2: one read; the deadline, the helper and the header all use it."""
    platform = HelperPlatform()
    reads = iter([700, 300, 300, 300])
    platform.hook_timeout_ms = lambda: next(reads)  # type: ignore[method-assign]  # changes
    world, _ = _world(platform, hook_helper=True)
    _tag, deadline, timeout = platform.helper_args
    assert timeout == 700
    assert deadline == pytest.approx(0.7 + 0.05)
    assert world.header(task())["hook_timeout_ms"] == "700"


@pytest.mark.parametrize("raises_from", [1, 2, 3])
def test_a_kill_read_that_raises_while_proving_is_monitor_lost(raises_from: int) -> None:
    """#385 HH4-01: every kill read goes through one guarded accessor; no kill-path read
    raises anything but `PortInterrupted`."""
    platform = HelperPlatform()
    world, _ = _world(platform, hook_helper=True)
    helper = platform.helper
    assert helper is not None
    helper.seen = False  # the probe is not seen yet: the proof polls
    helper.kill_reads = 0
    helper.kill_raises = RuntimeError("the helper is not started")
    helper.kill_raises_from = raises_from
    with pytest.raises(PortInterrupted) as stopped:
        world._prove_hooks()
    assert stopped.value.args == ("monitor_lost",)
    assert world.monitor_lost == 1


def test_a_helper_wait_that_returns_at_once_never_spins_the_sleep() -> None:
    """#385 HH4-03: an early return with no kill sleeps the rest of the step plainly."""
    platform = HelperPlatform()
    world, _ = _world(platform, hook_helper=True)
    helper = platform.helper
    assert helper is not None
    helper.returns_at_once = True
    started = world.clock()
    world._kill_aware_sleep(0.5)
    assert world.clock() - started >= 0.5
    assert len(helper.waits) <= round(0.5 / KILL_POLL_S) + 1


def test_every_hold_waits_on_the_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """#385 principal 3 (HH-08 scope): WorldPorts, the input guard and the act port pause
    through the helper for the whole time asked, never through a plain pause."""
    platform = HelperPlatform()
    world, _ = _world(platform, hook_helper=True)
    helper = platform.helper
    assert helper is not None
    real = ActPort
    pauses: list[Any] = []  # Any: the act port's pause callable

    def act_port(**kwargs: Any) -> Any:  # Any: the act port's own arguments
        pauses.append(kwargs["pause"])
        return real(**kwargs)

    monkeypatch.setattr(live_world, "ActPort", act_port)
    world.ports(task())
    plain: list[float] = []
    world._config = dataclasses.replace(world._config, pause=plain.append)  # records any
    for pause in (pauses[0], world.world._pause, world.desk._guard._pause):
        helper.waits.clear()
        before = world.clock()
        pause(0.12)
        assert sum(helper.waits) == pytest.approx(0.12)
        assert world.clock() - before == pytest.approx(0.12)
    assert plain == []


def test_a_kill_ends_a_helper_pause_early() -> None:
    platform = HelperPlatform()
    world, _ = _world(platform, hook_helper=True)
    helper = platform.helper
    assert helper is not None
    helper.kill_after_waits = 2
    world._pause(1.0)
    assert len(helper.waits) == 2


def test_the_helper_probe_is_bound_to_the_runs_tag() -> None:
    """#385 HH4-11: the probe callable carries the run's tag; it never reads the process-wide
    tag at call time, so a late probe after the restore still goes out with the run's tag."""
    sent: list[tuple[str, int]] = []

    def nudge(*, tag: int, seq: int) -> None:
        sent.append((f"nudge:{seq}", tag))

    def key_up(vk: int, *, tag: int, seq: int) -> None:
        sent.append((f"key_up:{vk:#x}:{seq}", tag))

    probe = live_world.bound_probe(0x2222_3333, vk=0x0E, nudge=nudge, key_up=key_up)
    probe(7)
    assert sent == [("nudge:7", 0x2222_3333), ("key_up:0xe:7", 0x2222_3333)]


def test_a_cleanup_that_raises_never_masks_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """#385 HH4-06: the helper's close failing after a failed build is noted on the refusal,
    and the rest of the cleanup still runs."""
    platform = HelperPlatform()
    original = platform.start_helper_monitor

    def start(*args: Any) -> HelperMonitor:  # Any: the platform's own arguments
        helper = original(*args)
        helper.close_raises = OSError(6, "the handle is invalid")
        return helper

    platform.start_helper_monitor = start  # type: ignore[method-assign,assignment]  # a failing close

    def broken(*_args: object) -> None:
        raise SteerError(ErrorCode.GATE_BLOCKED, "build_refused")

    monkeypatch.setattr(live_world.LiveWorld, "_build", broken)
    with pytest.raises(SteerError, match="build_refused") as refused:
        _world(platform, hook_helper=True)
    notes = getattr(refused.value, "__notes__", [])
    assert any("the handle is invalid" in n for n in notes)
    assert platform.calls.count("untag") == 1
    assert platform.calls.count("release_session") == 1


def test_close_noting_keeps_the_cause_and_notes_the_cleanup() -> None:
    cause = SteerError(ErrorCode.GATE_BLOCKED, "monitor_unavailable")

    def close() -> None:
        raise OSError(6, "the handle is invalid")

    live_world.close_noting(close, cause)
    assert any("the handle is invalid" in n for n in cause.__notes__)


@pytest.mark.windows
def test_the_windows_platform_runs_on_the_helper_end_to_end() -> None:
    """#385 principal 1, the real assembly (CI only: it starts the helper's hooks and sends the
    tagged probe): `WindowsPlatform` with `hook_helper`, on its own loop thread and the real
    clocks. The switch reads clean or a session token, both hooks see the run's tagged probe
    through the helper, and the helper is gone after the world closes."""
    from steerai.probes.windows import list_top_windows  # noqa: PLC0415 - Windows only
    from steerai.session.helper_launch import (  # noqa: PLC0415 - Windows only
        SYNCHRONIZE,
        HelperProcess,
        WindowsLaunch,
    )
    from steerai.session.helper_monitor import HelperMonitor as RealMonitor  # noqa: PLC0415

    hwnd = list_top_windows()[0].hwnd
    platform = live_world.WindowsPlatform()
    win32 = WindowsLaunch()
    watched: list[int] = []
    config = _config(
        hwnd=hwnd,
        kill_key="ctrl+alt+shift+f9",
        hook_helper=True,
        clock=time.monotonic,
        pause=time.sleep,
    )

    def task() -> object:
        with live_world.LiveWorld(config, platform) as world:
            monitor = world._helper_monitor
            assert isinstance(monitor, RealMonitor)
            helper = monitor._helper
            assert isinstance(helper, HelperProcess)
            watched.append(win32.duplicate(helper.process_handle, SYNCHRONIZE))
            token = world.interrupted()
            world._prove_hooks()  # returns: both hooks saw the tagged probe in time
            return token

    token = live_world.run_on_loop_thread(task, release=platform.release_com)
    assert token in {None, "session_locked", "secure_desktop"}
    (watch,) = watched
    try:
        assert win32.wait_any((watch,), 3000) == 0  # the helper is gone after close
    finally:
        win32.close(watch)


# ---- #398: a stale watchdog poll is the monitor's loss, never a session token -----------------


class RigPlatform(HelperPlatform):
    """The real `HelperMonitor` over the session tests' fake helper process."""

    def __init__(self, monitor: Any) -> None:  # Any: the real monitor, typed loosely here
        super().__init__()
        self.real = monitor

    def start_helper_monitor(self, *_args: Any) -> Any:  # Any: the platform's arguments
        return self.real


def test_a_stale_watchdog_poll_stops_the_task_as_monitor_lost() -> None:
    rig = _rig()
    rig.heartbeat()
    world, _ = _world(RigPlatform(rig.monitor), hook_helper=True)
    assert world.interrupted() is None
    rig.at(100.0 + DEADLINE_S + 0.01)
    rig.heartbeat()  # the helper is fine; the watchdog is not
    assert world.interrupted() == "monitor_lost"
    assert world.interrupted() == "monitor_lost"
    assert world.monitor_lost == 1


def test_after_the_helper_closes_the_switch_reads_monitor_lost() -> None:
    """#385 final review (3): a closed helper's stale poll is not a lock."""
    rig = _rig()
    rig.heartbeat()
    world, _ = _world(RigPlatform(rig.monitor), hook_helper=True)
    rig.monitor.close()
    rig.at(100.0 + 10 * DEADLINE_S)
    assert world.interrupted() == "monitor_lost"


def test_the_worlds_own_lock_read_still_wins_over_a_stale_poll() -> None:
    """LW-17/18: the lock is the cause; a monitor lost behind it is only a symptom."""
    rig = _rig()
    rig.heartbeat()
    platform = RigPlatform(rig.monitor)
    world, _ = _world(platform, hook_helper=True)
    rig.at(100.0 + DEADLINE_S + 0.01)
    platform.locked = True
    assert world.interrupted() == "session_locked"


# ---- #398 HH4-05, HH4-04: helper-mode holds are counted in steps, timed on the gap clock -------


class Fine(Clock):
    """The QPC-like gap clock; the world's clock reads it in 15.625 ms ticks."""

    def coarse(self) -> float:
        return math.floor(self.now / TICK_S) * TICK_S


TICK_S = 0.015625


def _ticked() -> tuple[LiveWorld, HelperMonitor, Fine]:
    fine = Fine()
    platform = HelperPlatform()
    world, _ = _world(
        platform, hook_helper=True, clock=fine.coarse, pause=fine.pause, gap_clock=fine
    )
    helper = platform.helper
    assert helper is not None
    helper.clock = fine
    return world, helper, fine


@pytest.mark.parametrize("hold", ["sleep", "pause"])
@pytest.mark.parametrize("seconds", [0.05, 0.12, 0.2])
def test_a_helper_hold_lasts_what_was_asked_on_a_ticking_world_clock(
    hold: str, seconds: float
) -> None:
    """HH4-05: a step is timed on the gap clock (QPC), never on the world's 15.625 ms clock,
    so a 50 ms step is not slept out to the next tick."""
    world, _, fine = _ticked()
    fine.now += 0.004  # between two ticks
    started = fine.now
    (world._kill_aware_sleep if hold == "sleep" else world._pause)(seconds)
    assert fine.now - started == pytest.approx(seconds)


@pytest.mark.parametrize("hold", ["sleep", "pause"])
def test_a_backwards_gap_clock_never_stretches_a_step(hold: str) -> None:
    """HH4-05: a helper wait that returns at once while the gap clock reads backwards sleeps at
    most the rest of its own step; the hold has as many steps as the time asked holds."""
    world, helper, fine = _ticked()
    helper.returns_at_once = True
    reads = iter(range(1000, 0, -1))
    world._config = dataclasses.replace(world._config, gap_clock=lambda: float(next(reads)))
    started = fine.now
    (world._kill_aware_sleep if hold == "sleep" else world._pause)(0.12)
    assert fine.now - started == pytest.approx(0.12)
    assert len(helper.waits) == math.ceil(0.12 / KILL_POLL_S)


@pytest.mark.parametrize(
    "seconds", [math.nan, math.inf, -math.inf, -1.0, 0.0, 0.051, 0.12, KILL_POLL_S]
)
def test_the_helper_sleep_takes_the_plain_sleeps_steps_and_reads(seconds: float) -> None:
    """HH4-04: NaN, infinities, a negative time and a last part-step (no overshoot) behave as
    `kill_sleep` does: the same steps, and the switch read before each and once at the end."""
    plain: list[float] = []
    plain_reads: list[None] = []

    def read() -> None:
        plain_reads.append(None)

    kill_sleep(seconds, interrupted=read, pause=plain.append)
    world, helper, _ = _ticked()
    reads = 0
    real = world.interrupted

    def counted() -> str | None:
        nonlocal reads
        reads += 1
        return real()

    world.interrupted = counted  # type: ignore[method-assign]  # counts the switch reads
    world._kill_aware_sleep(seconds)
    assert helper.waits == pytest.approx(plain)
    assert reads == len(plain_reads)
