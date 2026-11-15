"""[V2.60] hook helper, PR 3 (packet rev 2b §5.3-§5.6, §6.1): `HelperMonitor`, the in-process
`Monitor` protocol over the helper's words, plus the watchdog step.

Pure: a fake helper (two bytearrays), a fake session, injected clocks. The watchdog thread is
only a loop around `watch_once`, which these tests call directly."""

from __future__ import annotations

import ast
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from steerai.session import helper_monitor
from steerai.session.helper_core import KILL_ACK_S, write_config
from steerai.session.helper_launch import CLOSE_TIMEOUT_MS
from steerai.session.helper_monitor import WATCH_PERIOD_MS, HelperMonitor
from steerai.session.hook_layout import (
    HISTOGRAMS,
    KIND_OTHER_STEER,
    KIND_TAKEOVER,
    STATE_FAILED,
    STATE_READY,
    Words,
    config_size,
    config_words,
    pack_last_user,
    status_size,
    status_words,
)

FREQ = 10_000_000
PID = 4242
DEADLINE_S = 1.05


@dataclass
class FakeHelper:
    """A started helper: its words, its pid, liveness and waits."""

    status_buf: bytearray = field(default_factory=lambda: bytearray(status_size()))
    config_buf: bytearray = field(default_factory=lambda: bytearray(config_size()))
    exited: bool = False
    terminated: int = 0
    closed: int = 0
    wakes: list[int] = field(default_factory=list)
    wake_fails: bool = False
    on_get: Callable[[str], None] | None = None
    terminate_exits: bool = True
    """False: the end was asked for but has not taken effect yet (its words still read)."""

    def __post_init__(self) -> None:
        write_config(self.config, run_tag=0x1234_5678, probe_vk=0x0E, kill_mods=7, kill_vk=0x53,
                     dead_band_px=4, heartbeat_ms=100, hook_timeout_ms=1000,
                     nonce=(11, 22))  # fmt: skip
        s = self.status_w
        s.set("pid", PID)
        s.set("nonce_echo_lo", 11)
        s.set("nonce_echo_hi", 22)
        s.set("state", STATE_READY)

    @property
    def status(self) -> Words:
        if self.closed:
            raise RuntimeError("the helper is not started")  # as `HelperProcess` once released
        words = status_words(memoryview(self.status_buf).toreadonly())
        if self.on_get is None:
            return words
        hook = self.on_get

        class Spy(Words):
            def get(self, name: str) -> int:
                value = words.get(name)
                hook(name)
                return value

        return Spy(memoryview(self.status_buf).toreadonly(), words.names(), HISTOGRAMS)

    @property
    def status_w(self) -> Words:
        """The helper's own writable view, for the test to play the helper."""
        return status_words(memoryview(self.status_buf))

    @property
    def config(self) -> Words:
        return config_words(memoryview(self.config_buf))

    def pid(self) -> int:
        return PID

    def has_exited(self) -> bool:
        return self.exited

    def wait_exit(self, timeout_ms: int) -> bool:
        del timeout_ms
        return self.exited

    def wait_wake(self, timeout_ms: int) -> None:
        self.wakes.append(timeout_ms)
        if self.wake_fails:
            raise OSError(6, "the handle is invalid")

    def terminate(self) -> None:
        self.terminated += 1
        self.exited = self.terminate_exits

    def close(self) -> None:
        self.closed += 1


@dataclass
class FakeSession:
    lock: int | None = 1
    connect: tuple[int, int, int] | None = (0, 100, 0)
    desktop: str | None = "Default"

    def lock_state(self) -> int | None:
        return self.lock

    def connect_state(self) -> tuple[int, int, int] | None:
        return self.connect

    def input_desktop(self) -> str | None:
        return self.desktop


@dataclass
class Rig:
    helper: FakeHelper
    session: FakeSession
    monitor: HelperMonitor
    t: list[float]
    probes: list[int]
    ended: list[int]

    def at(self, seconds: float) -> None:
        """Both clocks move together: Steer's world clock and QPC."""
        self.t[0] = seconds

    def heartbeat(self) -> None:
        s = self.helper.status_w
        s.set("heartbeat_qpc", int(self.t[0] * FREQ))
        s.set("heartbeat_count", s.get("heartbeat_count") + 1)

    def deliver(self, *hooks: str) -> None:
        s = self.helper.status_w
        for hook in hooks:
            s.set(f"last_own_qpc_{hook}", int(self.t[0] * FREQ))
            s.set(f"delivered_{hook}", s.get(f"delivered_{hook}") + 1)


def _rig(*, probe_fails: bool = False, session: FakeSession | None = None) -> Rig:
    helper = FakeHelper()
    session = FakeSession() if session is None else session
    t = [100.0]
    probes: list[int] = []
    ended: list[int] = []

    def probe() -> None:
        if probe_fails:
            raise OSError(5, "UIPI")
        probes.append(1)

    monitor = HelperMonitor(
        helper,
        session=session,
        clock=lambda: t[0],
        qpc=lambda: int(t[0] * FREQ),
        freq=FREQ,
        deadline_s=DEADLINE_S,
        probe=probe,
        terminate_self=lambda: ended.append(1),
        start_thread=False,
    )
    rig = Rig(helper, session, monitor, t, probes, ended)
    rig.heartbeat()
    return rig


# ---- health ------------------------------------------------------------------------------------


def test_a_fresh_helper_is_healthy() -> None:
    assert _rig().monitor.healthy()


@pytest.mark.parametrize("broken", ["exited", "failed", "stale", "callback_errors", "gap"])
def test_a_helper_caused_fault_is_lost_and_the_helper_is_ended_at_once(broken: str) -> None:
    """P4 (rev 2): on a helper-caused loss, `TerminateJobObject` at once, from the watchdog."""
    rig = _rig()
    rig.monitor.watch_once()
    if broken == "exited":
        rig.helper.exited = True
    elif broken == "failed":
        rig.helper.status_w.set("state", STATE_FAILED)
    elif broken == "stale":
        rig.at(100.0 + DEADLINE_S + 0.1)
    elif broken == "callback_errors":
        rig.helper.status_w.set("callback_errors", 1)
    elif broken == "gap":
        rig.helper.status_w.set("gap_events", 1)
    rig.monitor.watch_once()
    assert rig.helper.terminated == 1
    assert not rig.monitor.healthy()
    rig.heartbeat()
    assert not rig.monitor.healthy()  # latched: never healthy again (K3)


def test_the_loop_threads_own_read_also_loses_and_ends_the_helper() -> None:
    rig = _rig()
    rig.helper.status_w.set("callback_errors", 1)
    assert not rig.monitor.healthy()
    assert rig.helper.terminated == 1


def test_a_watchdog_read_in_the_middle_of_a_loop_read_is_no_false_decrease() -> None:
    """Rev 2a: the loop thread has read `delivered_mouse` (old) when the helper moves on and the
    watchdog reads the newer words. Were the last-read values shared, the loop's older value
    would look like a decrease and the monitor would be lost for nothing."""
    rig = _rig()
    rig.monitor.watch_once()
    assert rig.monitor.healthy()
    fired: list[int] = []

    def mid_read(name: str) -> None:
        if name == "callback_errors" and not fired:  # the last word of the loop's read
            fired.append(1)
            rig.deliver("mouse")
            rig.monitor.watch_once()

    rig.helper.on_get = mid_read
    assert rig.monitor.healthy()
    assert fired
    assert rig.helper.terminated == 0


def test_the_loop_thread_and_the_watchdog_never_share_last_read_values() -> None:
    """Rev 2a: interleaved readers over words that only grow never lose the monitor."""
    rig = _rig()
    for i in range(1, 6):
        rig.at(100.0 + 0.05 * i)
        rig.heartbeat()
        if i % 2:
            rig.monitor.watch_once()
            assert rig.monitor.healthy()
        else:
            assert rig.monitor.healthy()
            rig.monitor.watch_once()
    assert rig.helper.terminated == 0


# ---- K1: the probe ledger over the words -------------------------------------------------------


def test_a_probe_is_proven_only_when_both_hooks_delivered_it() -> None:
    rig = _rig()
    rig.monitor.probe_now()
    assert rig.probes == [1]
    rig.at(100.01)
    rig.deliver("mouse")
    assert not rig.monitor.probe_seen()
    rig.deliver("keyboard")
    assert rig.monitor.probe_seen()


def test_a_hook_that_missed_the_deadline_loses_the_monitor() -> None:
    rig = _rig()
    rig.monitor.probe_now()
    rig.at(100.0 + DEADLINE_S + 0.01)
    rig.heartbeat()
    rig.deliver("mouse")
    assert not rig.monitor.healthy()


def test_a_probe_that_cannot_be_sent_loses_the_monitor() -> None:
    rig = _rig(probe_fails=True)
    rig.monitor.probe_now()
    assert not rig.monitor.healthy()


def test_maintain_probes_only_when_due() -> None:
    rig = _rig()
    rig.deliver("mouse", "keyboard")
    rig.monitor.maintain()
    assert rig.probes == []
    rig.at(101.5)
    rig.heartbeat()
    rig.monitor.maintain()
    assert rig.probes == [1]


# ---- K4, [V2.60] Q1: the kill and its layers -----------------------------------------------------


def test_the_first_kill_seen_by_the_loop_is_acknowledged() -> None:
    """Layer 1: the loop thread writes `kill_ack` the first time it sees the kill."""
    rig = _rig()
    assert not rig.monitor.kill_requested()
    rig.helper.status_w.set("kill", 1)
    assert rig.monitor.kill_requested()
    assert rig.helper.config.get("kill_ack") == 1


def test_an_unacknowledged_kill_makes_the_watchdog_end_steer_after_the_bound() -> None:
    """Layer 2: the watchdog ends Steer when the loop never acknowledged a set kill."""
    rig = _rig()
    rig.helper.status_w.set("kill", 1)
    rig.monitor.watch_once()
    rig.at(100.0 + KILL_ACK_S)
    rig.monitor.watch_once()
    assert rig.ended == []
    rig.at(100.0 + KILL_ACK_S + 0.01)
    rig.monitor.watch_once()
    rig.monitor.watch_once()
    assert rig.ended == [1]  # once


def test_an_acknowledged_kill_never_ends_steer() -> None:
    rig = _rig()
    rig.helper.status_w.set("kill", 1)
    rig.monitor.watch_once()
    assert rig.monitor.kill_requested()
    rig.at(100.0 + 10 * KILL_ACK_S)
    rig.monitor.watch_once()
    assert rig.ended == []


def test_a_kill_wins_even_over_a_dead_helper() -> None:
    rig = _rig()
    rig.helper.status_w.set("kill", 1)
    rig.helper.exited = True
    assert rig.monitor.kill_requested()
    rig.monitor.watch_once()
    assert rig.helper.terminated == 0  # a kill is not a loss


def test_only_the_kill_layer_ends_steer() -> None:
    """[V2.60] Q1: no other path in the monitor ends Steer's own process."""
    tree = ast.parse(Path(helper_monitor.__file__).read_text(encoding="utf-8"))
    callers = [
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_terminate_self"
    ]
    assert callers == ["_kill_layer"]


# ---- §5.3: the person's input ---------------------------------------------------------------


def test_the_persons_last_input_is_placed_by_its_age() -> None:
    rig = _rig()
    assert rig.monitor.last_user_input() == 100.0  # none yet: the monitor's start
    rig.helper.status_w.set("last_user", pack_last_user(int(99.0 * FREQ), KIND_TAKEOVER))
    assert rig.monitor.last_user_input() == pytest.approx(99.0 + 0.015625)
    assert rig.monitor.takeover_token() == "takeover"
    rig.helper.status_w.set("last_user", pack_last_user(int(99.5 * FREQ), KIND_OTHER_STEER))
    assert rig.monitor.takeover_token() == "other_steer_input"


# ---- §5.6 / HH-02: session and desktop transitions ---------------------------------------------


@pytest.mark.parametrize(
    ("change", "token"),
    [
        ({"lock": 0}, "session_locked"),
        ({"connect": (0, 200, 0)}, "session_locked"),
        ({"desktop": "Winlogon"}, "secure_desktop"),
        ({"lock": None}, "session_locked"),
        ({"desktop": None}, "secure_desktop"),
    ],
)
def test_a_session_or_desktop_transition_during_the_task_stops_it(
    change: dict[str, object], token: str
) -> None:
    rig = _rig()
    rig.monitor.watch_once()
    assert rig.monitor.session_change() is None
    for name, value in change.items():
        setattr(rig.session, name, value)
    rig.monitor.watch_once()
    assert rig.monitor.session_change() == token


def test_a_lock_and_unlock_between_two_reads_still_counts() -> None:
    """A point-in-time read is not enough: the transition is counted, not the state."""
    rig = _rig()
    rig.monitor.watch_once()
    rig.session.lock = 0
    rig.monitor.watch_once()
    rig.session.lock = 1
    rig.monitor.watch_once()
    assert rig.monitor.session_change() == "session_locked"


# ---- the rest of the protocol ------------------------------------------------------------------


def test_latency_is_read_from_the_helpers_histogram() -> None:
    rig = _rig()
    s = rig.helper.status_w
    for ms in [0] * 95 + [16] * 4 + [32]:
        s.bump_bucket("latency", ms)
    s.set("latency_events", 100)
    assert rig.monitor.latency() == (100, 0, 16)


def test_a_kill_aware_wait_waits_on_the_helper() -> None:
    rig = _rig()
    rig.monitor.wait(0.25)
    assert rig.helper.wakes == [250]


def test_close_closes_the_helper_once() -> None:
    rig = _rig()
    rig.monitor.close()
    rig.monitor.close()
    assert rig.helper.closed == 1


def test_any_nonzero_kill_word_is_requested() -> None:
    """#383 HM-06."""
    rig = _rig()
    rig.helper.status_w.set("kill", 7)
    assert rig.monitor.kill_requested()


def test_a_latched_loss_stays_lost_when_the_words_read_well_again() -> None:
    """#383 T1: the helper is being ended but its words still read; a heartbeat that goes
    fresh again must not make a lost monitor healthy (K3)."""
    rig = _rig()
    rig.helper.terminate_exits = False
    rig.at(100.0 + DEADLINE_S + 0.1)
    assert not rig.monitor.healthy()  # stale: lost, latched
    rig.heartbeat()
    assert not rig.monitor.healthy()


# ---- #383 blocker 4: the watchdog thread's loop -------------------------------------------------


def _stop_after(monitor: HelperMonitor, rounds: int) -> list[float | None]:
    """Replace the stop event's wait: record each timeout, stop after `rounds`."""
    waits: list[float | None] = []
    stop = monitor._stop

    def wait(timeout: float | None = None) -> bool:
        waits.append(timeout)
        if len(waits) >= rounds:
            stop.set()
        return stop.is_set()

    stop.wait = wait  # type: ignore[method-assign]  # a counted wait
    return waits


def test_once_the_helper_is_gone_the_watchdog_waits_its_period_and_never_spins() -> None:
    """A signalled process handle makes `wait_exit` return at once; the loop then waits on its
    own stop event for one period instead."""
    rig = _rig()
    rig.helper.exited = True
    waits = _stop_after(rig.monitor, 3)
    rig.monitor._run()
    assert waits == [WATCH_PERIOD_MS / 1000] * 3
    assert rig.helper.terminated == 1  # the dead helper is a loss, latched once


def test_a_raise_anywhere_in_the_watchdog_is_a_loss_never_a_silent_death() -> None:
    rig = _rig()

    def broken(timeout_ms: int) -> bool:
        raise OSError(6, "the handle is invalid")

    rig.helper.wait_exit = broken  # type: ignore[method-assign]  # the wait itself fails
    waits = _stop_after(rig.monitor, 2)
    rig.monitor._run()
    assert not rig.monitor.healthy()
    assert waits  # it kept watching, without spinning


# ---- #383 blocker 5: the session reads fail closed ---------------------------------------------


@pytest.mark.parametrize(
    ("start", "token"),
    [
        ({"lock": 0}, "session_locked"),  # HM-01: already locked when the task starts
        ({"desktop": "Winlogon"}, "secure_desktop"),  # HM-01: already on the secure desktop
        ({"lock": None}, "session_locked"),  # T3: a first read that is unread
        ({"desktop": None}, "secure_desktop"),
        ({"connect": None}, "session_locked"),
    ],
)
def test_the_first_poll_at_construction_flags_a_state_that_is_not_ours(
    start: dict[str, object], token: str
) -> None:
    session = FakeSession()
    for name, value in start.items():
        setattr(session, name, value)
    rig = _rig(session=session)
    assert rig.monitor.session_change() == token  # before any watchdog step


def test_a_session_poll_older_than_the_deadline_is_a_stop() -> None:
    """A dead or hung watchdog (HM-03), or a GIL stall (HM-02): the last poll is too old, and
    what it said can no longer be trusted."""
    rig = _rig()
    assert rig.monitor.session_change() is None
    rig.at(100.0 + DEADLINE_S + 0.01)
    assert rig.monitor.session_change() == "session_locked"


def test_a_stale_session_poll_loses_the_monitor() -> None:
    rig = _rig()
    rig.at(100.0 + DEADLINE_S * 0.5)
    rig.heartbeat()
    assert rig.monitor.healthy()
    rig.at(100.0 + DEADLINE_S + 0.01)
    rig.heartbeat()  # the helper is fine; the watchdog is not
    assert not rig.monitor.healthy()


def test_each_watchdog_poll_refreshes_the_poll_age() -> None:
    rig = _rig()
    for i in range(1, 6):
        rig.at(100.0 + i * DEADLINE_S * 0.6)
        rig.heartbeat()
        rig.monitor.watch_once()
        assert rig.monitor.session_change() is None


# ---- #383 blocker 6: close never races a watchdog that is still ending the helper -------------


def test_close_joins_the_watchdog_for_longer_than_ending_the_helper_can_take() -> None:
    rig = _rig()
    joins: list[float | None] = []

    class Thread:
        def join(self, timeout: float | None = None) -> None:
            joins.append(timeout)

    rig.monitor._thread = Thread()  # type: ignore[assignment]  # records the join's timeout
    rig.monitor.close()
    (timeout,) = joins
    assert timeout is not None
    assert timeout > CLOSE_TIMEOUT_MS / 1000 + WATCH_PERIOD_MS / 1000
    assert rig.helper.closed == 1


@dataclass
class HeldHelper(FakeHelper):
    """A helper whose end is held, as a real one is for up to `CLOSE_TIMEOUT_MS`."""

    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    order: list[str] = field(default_factory=list)

    def terminate(self) -> None:
        self.entered.set()
        self.release.wait(5)
        super().terminate()
        self.order.append("terminated")

    def close(self) -> None:
        self.order.append("closed")
        super().close()


def test_close_waits_for_a_watchdog_still_ending_the_helper() -> None:
    helper = HeldHelper()
    helper.exited = True  # the watchdog's first step is a loss, and it ends the helper
    monitor = HelperMonitor(
        helper,
        session=FakeSession(),
        clock=lambda: 100.0,
        qpc=lambda: int(100.0 * FREQ),
        freq=FREQ,
        deadline_s=DEADLINE_S,
        probe=lambda: None,
        terminate_self=lambda: None,
    )
    assert helper.entered.wait(5)
    closer = threading.Thread(target=monitor.close)
    closer.start()
    closer.join(0.2)
    assert closer.is_alive()
    assert helper.closed == 0
    helper.release.set()
    closer.join(5)
    assert helper.order == ["terminated", "closed"]


# ---- #383 HM-08: a failed self-end (layer 2) is counted and tried again -----------------------


def test_a_failed_self_end_is_counted_and_tried_again_on_the_next_step() -> None:
    rig = _rig()
    failures = [OSError(5, "denied")]

    def end() -> None:
        if failures:
            raise failures.pop()
        rig.ended.append(1)

    rig.monitor._terminate_self = end
    rig.helper.status_w.set("kill", 1)
    rig.monitor.watch_once()
    rig.at(100.0 + KILL_ACK_S + 0.01)
    rig.heartbeat()
    rig.monitor.watch_once()
    assert rig.ended == []
    assert rig.helper.config.get("end_self_failures") == 1
    rig.monitor.watch_once()
    rig.monitor.watch_once()
    assert rig.ended == [1]  # tried again, then once only
    assert rig.helper.config.get("end_self_failures") == 1


# ---- #383 HM-20: a kill-aware wait is clamped, and never returns at once to a caller's loop ---


def _naps(monitor: HelperMonitor) -> list[float | None]:
    naps: list[float | None] = []

    def wait(timeout: float | None = None) -> bool:
        naps.append(timeout)
        return False

    monitor._nap.wait = wait  # type: ignore[method-assign]  # a recorded nap
    return naps


@pytest.mark.parametrize(
    ("seconds", "ms"),
    [(float("nan"), 0), (-1.0, 0), (float("inf"), 1000), (1e12, 1000), (0.0004, 0)],
)
def test_a_kill_aware_wait_is_clamped(seconds: float, ms: int) -> None:
    rig = _rig()
    rig.monitor.wait(seconds)
    assert rig.helper.wakes == [ms]


def test_once_the_helper_is_gone_a_wait_naps_its_time_instead_of_returning_at_once() -> None:
    """Its exit signals the wake wait at once: a caller's loop of waits would spin."""
    rig = _rig()
    naps = _naps(rig.monitor)
    rig.helper.exited = True
    rig.monitor.wait(0.25)
    assert rig.helper.wakes == []
    assert naps == [0.25]


def test_after_close_a_wait_naps_and_never_touches_the_helper() -> None:
    rig = _rig()
    naps = _naps(rig.monitor)
    rig.monitor.close()
    rig.monitor.wait(0.25)
    assert rig.helper.wakes == []
    assert naps == [0.25]


def test_a_failed_wake_wait_is_a_loss_and_naps_its_time() -> None:
    rig = _rig()
    naps = _naps(rig.monitor)
    rig.helper.wake_fails = True
    rig.monitor.wait(0.25)
    assert naps == [0.25]
    assert not rig.monitor.healthy()


# ---- #383 principal note 1 (PR 4): nothing reads the words after close -------------------------


def test_after_close_the_monitor_is_lost_and_reads_no_words() -> None:
    """The real words are unmapped by then: a read would be an access violation."""
    rig = _rig()
    rig.monitor.close()
    assert not rig.monitor.healthy()
    rig.monitor.maintain()
    assert not rig.monitor.probe_seen()
    assert rig.probes == []
    assert rig.helper.terminated == 0  # a close is not a helper-caused loss


def test_the_counts_fetch_the_words_on_every_read() -> None:
    """Never a view kept from construction (a stale mapping)."""
    rig = _rig()
    fresh = bytearray(len(rig.helper.status_buf))
    rig.helper.status_buf = fresh
    rig.helper.status_w.set("delivered_keyboard", 5)
    assert rig.monitor._counts.delivered("keyboard") == 5
