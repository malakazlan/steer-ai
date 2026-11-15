"""[V2.60] hook helper, PR 4: `LiveWorld` over `HelperMonitor`, behind `LiveConfig.hook_helper`.

With the flag off nothing changes (the in-process `HookMonitor`). With it on, the platform starts
the helper monitor in the same place in the start order (after the session and the run's tag);
the world reads its session and desktop transitions as stops (§5.6), and its kill-aware sleeps
wait on the helper (wake or exit, HH-08) instead of a plain sleep. Fakes only."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2 import live_world
from steerai.loop2.act_port import ActPort
from steerai.loop2.loop import PortInterrupted
from steerai.loop2.world_ports import KILL_POLL_S
from tests.loop2.test_live_world import Monitor, Platform, _world
from tests.loop2.world_fakes import task


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

    def nudge(*, tag: int) -> None:
        sent.append(("nudge", tag))

    def key_up(vk: int, *, tag: int) -> None:
        sent.append((f"key_up:{vk:#x}", tag))

    probe = live_world.bound_probe(0x2222_3333, vk=0x0E, nudge=nudge, key_up=key_up)
    probe()
    assert sent == [("nudge", 0x2222_3333), ("key_up:0xe", 0x2222_3333)]
