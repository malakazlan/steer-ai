"""The takeover monitor over the hook helper ([V2.60], packet v2-hook-helper rev 2b §4-§6.1): the
in-process `Monitor` protocol that `LiveWorld` reads, built on the helper's shared words.

- **Reads** go through `read_status` (§5.4): `kill` first, then every rule. The loop thread and the
  watchdog each own their last-read values (rev 2a).
- **The probe ledger** (K1) is the in-process one, over the words: per hook, by delivery counts.
- **A helper-caused loss** (dead, stale, failed, a callback error, a gap, a decrease, a hook that
  missed its deadline) is latched for the task and the helper is ended at once (P4); it is never
  restarted (K3).
- **The watchdog** (a daemon thread around `watch_once`) does the reads that must not run on the
  helper's hook thread ([V2.60] Q3): the session (WTS) and the input desktop. It counts their
  transitions; any during the task stops it (§5.6). It also runs kill layer 2.
- **The kill** ([V2.60] Q1): layer 1, the loop thread acknowledges (`kill_ack`) the first time it
  sees the kill; layer 2, the watchdog ends Steer's own process when a set kill stays
  unacknowledged past `KILL_ACK_S`. Nothing else here ever ends Steer (tested). A self-end that
  raises is counted in `end_self_failures` and tried again on the next step (#383 HM-08).
- **The ack disarms layers 2 and 3** (#383 HM-05, accepted as designed): once `kill_ack` is
  written neither fires, even if the loop then hangs. After the ack the kill is still read before
  every input burst ([V2.40]), so a loop that keeps running cannot send; layers 2 and 3 exist for
  a Steer that cannot read the kill at all.
"""

from __future__ import annotations

import ctypes
import math
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any, Final, Protocol

from steerai.session.helper_core import KILL_ACK_S, KILL_EXIT_CODE
from steerai.session.helper_launch import CLOSE_TIMEOUT_MS
from steerai.session.helper_reader import (
    ReaderState,
    StatusRead,
    is_other_steer,
    last_user_seconds,
    read_status,
)
from steerai.session.hook_classifier import HOOKS, OTHER_STEER_INPUT, TAKEOVER, ProbeTags
from steerai.session.hook_layout import LATENCY_BUCKETS, RETIRED_WORDS, Words
from steerai.session.probe_ledger import ProbeLedger

CLOCK_RESOLUTION_S: Final = 0.015625
"""`time.monotonic()` on CPython 3.12 here is GetTickCount64 at 15.625 ms (meas,
`time.get_clock_info`, 2026-11-15): one tick added to a person's input time (§5.3)."""
WATCH_PERIOD_MS: Final = 100
MAX_WAIT_MS: Final = 1000
"""#383 HM-20: one kill-aware wait is a step; its caller re-reads the words between steps."""
WATCH_JOIN_S: Final = (CLOSE_TIMEOUT_MS + 2 * WATCH_PERIOD_MS) / 1000
"""#383 blocker 6: close waits out a watchdog that may be ending the helper (up to
`CLOSE_TIMEOUT_MS`) before the helper's handles are closed."""
"""The watchdog's period: the helper's heartbeat period (rev 2 §5.3, provisional)."""
_UNSET: Final = object()
_OURS: Final[dict[str, object]] = {"lock": 1, "desktop": "Default"}
"""HM-01: the state a task may start in: unlocked (WTS_SESSIONSTATE_UNLOCK, 1) on the Default
desktop. Anything else at the first poll is a transition already."""


class HelperView(Protocol):
    """What the monitor needs from a started helper (`helper_launch.HelperProcess`)."""

    @property
    def status(self) -> Words: ...
    @property
    def config(self) -> Words: ...
    def pid(self) -> int: ...
    def has_exited(self) -> bool: ...
    def wait_exit(self, timeout_ms: int) -> bool: ...
    def wait_wake(self, timeout_ms: int) -> None: ...
    def terminate(self) -> None: ...
    def close(self) -> None: ...


class SessionReads(Protocol):
    """The session and desktop reads (§5.6); None is unread, and counts as a transition."""

    def lock_state(self) -> int | None: ...
    def connect_state(self) -> tuple[int, int, int] | None: ...
    def input_desktop(self) -> str | None: ...


class _WordCounts:
    """`ProbeLedger`'s view of the words: delivery counts and own-event times in seconds on the
    QPC timeline."""

    def __init__(self, helper: HelperView, freq: int) -> None:
        self._helper = helper  # the words fetched on every read, never kept (#383 note 1)
        self._freq = freq

    def proof(self, hook: str) -> int:
        return self._helper.status.get(f"proof_{hook}")

    def proof_at(self, hook: str) -> float:
        return self._helper.status.get(f"proof_qpc_{hook}") / self._freq

    def last_own(self, hook: str) -> float:
        return self._helper.status.get(f"last_own_qpc_{hook}") / self._freq

    def last_steer(self) -> float:
        return max(self.last_own(h) for h in HOOKS)


class HelperMonitor:
    """`LiveWorld.Monitor` over the hook helper."""

    def __init__(  # noqa: PLR0913 - every collaborator injected so the monitor runs pure in tests
        self,
        helper: HelperView,
        *,
        session: SessionReads,
        clock: Callable[[], float],
        qpc: Callable[[], int],
        freq: int,
        deadline_s: float,
        probe: Callable[[int], None],
        terminate_self: Callable[[], None],
        clock_resolution: float = CLOCK_RESOLUTION_S,
        start_thread: bool = True,
    ) -> None:
        self._helper = helper
        self._session = session
        self._clock = clock
        self._qpc = qpc
        self._freq = freq
        self._deadline_s = deadline_s
        self._probe = probe
        self._terminate_self = terminate_self
        self._resolution = clock_resolution
        self._started_at = clock()
        config = helper.config
        self._nonce = (config.get("nonce_lo"), config.get("nonce_hi"))
        self._pid = helper.pid()
        self._counts = _WordCounts(helper, freq)
        self._ledger = ProbeLedger(per_hook=True, deadline_s=deadline_s)
        self._tags = ProbeTags(config.get("run_tag"))
        self._loop_state = ReaderState()
        self._watch_state = ReaderState()
        self._lock = threading.Lock()
        self._lost: str | None = None
        self._probe_failed = False
        self._acked = False
        self._kill_seen_at: float | None = None
        self._ended_steer = False
        self._session_seen: dict[str, object] = dict.fromkeys(("lock", "connect", "desktop"),
                                                              _UNSET)  # fmt: skip
        self._transitions = dict.fromkeys(("lock", "connect", "desktop"), 0)
        self._polled_at = self._started_at
        self._closed = False
        self._poll_session()  # #383 blocker 5: never "nothing seen yet"
        self._stop = threading.Event()
        self._nap = threading.Event()
        """Never set: a wait that cannot wait on the helper still takes its time (HM-20)."""
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(target=self._run, name="steer-helper-watch",
                                            daemon=True)  # fmt: skip
            self._thread.start()

    # ---- what the loop reads --------------------------------------------------------------------

    def kill_requested(self) -> bool:
        """K4: the kill word, read directly; layer 1 acknowledges it the first time. Once
        closed nothing is read (#385 HH4-14): False, and the "closed" loss stops the task."""
        if self._closed:
            return False
        if self._helper.status.get("kill") == 0:  # #383 HM-06: nonzero is a kill
            return False
        if not self._acked:
            self._helper.config.set("kill_ack", 1)
            self._acked = True
        return True

    def healthy(self) -> bool:
        if self._lost is not None:
            return False
        read = self._read(self._loop_state)
        if read.killed:
            return True  # a kill is not a loss; `kill_requested` reports it
        if read.fault is None and self._probe_failed:
            return False
        cause = read.fault if read.fault is not None else self._watch_loss()
        if cause is not None:
            self._lose(cause)
            return False
        return True

    def _watch_loss(self) -> str | None:
        if self._poll_age() > self._deadline_s:  # HM-03: the watchdog stopped watching
            return "stale_session_poll"
        lost = self._ledger.lost_hooks(now=self._now(), counts=self._counts)
        return "lost_hooks:" + ",".join(lost) if lost else None

    def maintain(self) -> None:
        if self._closed:
            return  # #383 note 1: the words are unmapped once closed
        if self._ledger.probe_due(now=self._now(), counts=self._counts):
            self.probe_now()

    def probe_now(self) -> None:
        if self._closed:
            return  # #385 HH4-14: no probe for a closed helper
        try:
            tag = self._ledger.probe_sent(now=self._now(), begin=self._publish_probe_tag)
            self._probe(tag)
        except (OSError, RuntimeError):  # a failed send, or no tag could be drawn
            self._probe_failed = True  # fails closed: the next health check is false

    def _publish_probe_tag(self) -> int:
        """[V2.81]: the fresh tag into the shared word before the probe is injected, never
        after: the helper compares each event with it."""
        tag = self._tags.draw()
        config = self._helper.config
        ring = self._tags.retired()
        for i, name in enumerate(RETIRED_WORDS):  # the ring first: the helper reads it when the
            config.set(name, ring[i] if i < len(ring) else 0)  # tag changes, so it is ready
        config.set("probe_tag", tag)  # x64 stores are not reordered with each other (TSO)
        return tag

    def lost_hooks(self) -> tuple[str, ...]:
        """The hooks that missed the last probe past its deadline (the start self-check reads
        it to tell an unproven first probe from a fault of the monitor)."""
        if self._closed:
            return ()
        return self._ledger.lost_hooks(now=self._now(), counts=self._counts)

    def probe_seen(self) -> bool:
        return not self._closed and self._ledger.probe_seen(counts=self._counts)

    def last_user_input(self) -> float:
        if self._closed:
            return self._clock()  # #385 HH4-14: input "just now", so every send is refused
        word = self._helper.status.get("last_user")  # HH-10: the word first, then the clocks
        return last_user_seconds(word=word, qpc_now=self._qpc(), freq=self._freq,
                                 clock_now=self._clock(), clock_resolution=self._resolution,
                                 started_at=self._started_at)  # fmt: skip

    def takeover_token(self) -> str:
        if self._closed:
            return TAKEOVER
        return (
            OTHER_STEER_INPUT if is_other_steer(self._helper.status.get("last_user")) else TAKEOVER
        )

    def session_change(self) -> str | None:
        """§5.6: a lock, connect or desktop transition during the task, as its stop token. A
        last poll older than the deadline still stops the task (#383 blocker 5), as the
        monitor's loss (`healthy`: `stale_session_poll`), never as a lock this poll did not see
        (#398); the world's own lock read is taken before this one (LW-17/18)."""
        if self._poll_age() > self._deadline_s:
            return None
        if self._transitions["lock"] or self._transitions["connect"]:
            return "session_locked"
        if self._transitions["desktop"]:
            return "secure_desktop"
        return None

    def latency(self) -> tuple[int, int | None, int | None]:
        """The helper's latency histogram: (events, p95 ms, p99 ms); none once closed."""
        if self._closed:
            return 0, None, None
        status = self._helper.status
        events = status.get("latency_events")
        if not events:
            return 0, None, None
        return events, self._quantile(status, 0.95, events), self._quantile(status, 0.99, events)

    def wait(self, seconds: float) -> None:
        """HH-08: a kill-aware wait wakes on the helper's `wake` or its exit. #383 HM-20: the
        time is clamped; once the helper is closed or gone, or its wait fails, the time is
        napped instead, so a caller's loop of waits never spins."""
        ms = math.ceil(min(seconds, MAX_WAIT_MS / 1000) * 1000) if seconds > 0 else 0
        if self._closed:
            self._nap.wait(ms / 1000)
            return
        try:
            if self._helper.has_exited():
                self._nap.wait(ms / 1000)
                return
            self._helper.wait_wake(ms)
        except OSError as exc:  # a liveness read or the wait itself failed (#385)
            self._lose(f"wake_wait_failed:{exc.errno}")
            self._nap.wait(ms / 1000)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            if self._lost is None:
                self._lost = "closed"  # #383 note 1: latched, so no health check reads the words
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=WATCH_JOIN_S)
        self._helper.close()

    # ---- the watchdog ----------------------------------------------------------------------------

    def watch_once(self) -> None:
        """One watchdog step: its own validated read, the kill layer, the session reads."""
        if self._closed:
            return
        read = self._read(self._watch_state)
        if read.killed:
            self._kill_layer()
            return
        if read.fault is not None:
            self._lose(read.fault)
        self._poll_session()

    def _run(self) -> None:
        """The watchdog thread: one `watch_once` per period. Once the helper has exited its
        handle is signalled and `wait_exit` returns at once, so the period is then waited on the
        stop event (#383 blocker 4). The whole body is guarded: a raise is a loss, latched,
        never a silent death of the watchdog."""
        period_s = WATCH_PERIOD_MS / 1000
        while not self._stop.is_set():
            try:
                if self._helper.wait_exit(WATCH_PERIOD_MS):
                    self._stop.wait(period_s)
                if self._stop.is_set():
                    return
                self.watch_once()
            except Exception:  # noqa: BLE001 - a watchdog that cannot watch is a loss, latched
                self._lose("watchdog_error")
                self._stop.wait(period_s)

    def _kill_layer(self) -> None:
        """[V2.60] Q1, layer 2: a set kill the loop never acknowledged ends Steer itself."""
        if self._helper.config.get("kill_ack"):
            return
        now = self._clock()
        if self._kill_seen_at is None:
            self._kill_seen_at = now
        if now - self._kill_seen_at > KILL_ACK_S and not self._ended_steer:
            try:
                self._terminate_self()
            except Exception:  # noqa: BLE001 - #383 HM-08: counted in Steer's word, tried again
                config = self._helper.config
                config.set("end_self_failures", config.get("end_self_failures") + 1)
                return
            self._ended_steer = True

    def _poll_session(self) -> None:
        reads = {
            "lock": self._session.lock_state(),
            "connect": self._session.connect_state(),
            "desktop": self._session.input_desktop(),
        }
        for name, value in reads.items():
            seen = self._session_seen[name]
            first_not_ours = seen is _UNSET and value != _OURS.get(name, value)
            if value is None or first_not_ours or (seen is not _UNSET and value != seen):
                self._transitions[name] += 1
            self._session_seen[name] = value
        self._polled_at = self._clock()

    def _poll_age(self) -> float:
        return self._clock() - self._polled_at

    # ---- internals -----------------------------------------------------------------------------

    def _now(self) -> float:
        return self._qpc() / self._freq

    def _read(self, state: ReaderState) -> StatusRead:
        return read_status(
            self._helper.status,
            state,
            qpc_now=self._qpc,
            freq=self._freq,
            deadline_s=self._deadline_s,
            nonce=self._nonce,
            pid=self._pid,
            helper_exited=self._helper.has_exited(),
        )

    def _lose(self, reason: str) -> None:
        """P4: latch the loss for the task and end the helper at once, once."""
        with self._lock:
            if self._lost is not None:
                return
            self._lost = reason
        try:
            self._helper.terminate()
        except OSError as exc:  # #388: the end was asked for, its wait failed; recorded
            with self._lock:
                self._lost = f"{reason};end_unconfirmed:{exc.errno}"

    @staticmethod
    def _quantile(status: Words, q: float, events: int) -> int:
        rank = math.ceil(q * events)
        seen = 0
        for ms in range(LATENCY_BUCKETS):
            seen += status.bucket("latency", ms)
            if seen >= rank:
                return ms
        return LATENCY_BUCKETS - 1


# ---- the Windows clocks and the last kill layer's exit (Windows only) ---------------------------


def qpc_clock() -> tuple[Callable[[], int], int]:  # pragma: no cover - live, Windows only
    """QueryPerformanceCounter and its frequency (Microsoft Learn: fixed at boot)."""
    k: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # Any: a ctypes DLL
    k.QueryPerformanceCounter.argtypes = (ctypes.POINTER(ctypes.c_int64),)
    k.QueryPerformanceCounter.restype = wintypes.BOOL
    k.QueryPerformanceFrequency.argtypes = (ctypes.POINTER(ctypes.c_int64),)
    k.QueryPerformanceFrequency.restype = wintypes.BOOL
    freq = ctypes.c_int64()
    if not k.QueryPerformanceFrequency(ctypes.byref(freq)) or freq.value <= 0:
        raise OSError(ctypes.get_last_error(), "QueryPerformanceFrequency failed")

    def qpc() -> int:
        value = ctypes.c_int64()
        k.QueryPerformanceCounter(ctypes.byref(value))
        return int(value.value)

    return qpc, int(freq.value)


def end_this_process() -> None:  # pragma: no cover - live, Windows only
    """[V2.60] Q1 layer 2: end Steer for the person's kill that its loop never acknowledged."""
    k: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # Any: a ctypes DLL
    k.GetCurrentProcess.argtypes = ()
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k.TerminateProcess.restype = wintypes.BOOL
    k.TerminateProcess(k.GetCurrentProcess(), KILL_EXIT_CODE)


# ---- the session and desktop reads (Windows only; on the watchdog thread) -----------------------

WTS_CURRENT_SERVER_HANDLE: Final = 0
WTS_CURRENT_SESSION: Final = 0xFFFF_FFFF
WTS_SESSION_INFO_EX: Final = 25
DESKTOP_READOBJECTS: Final = 0x0001
UOI_NAME: Final = 2


class _WtsInfoLevel1(ctypes.Structure):
    """WTSINFOEX_LEVEL1_W (Microsoft Learn). The names are read into memory, never kept."""

    _fields_ = (
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * 33),
        ("UserName", wintypes.WCHAR * 21),
        ("DomainName", wintypes.WCHAR * 18),
        ("LogonTime", ctypes.c_int64),
        ("ConnectTime", ctypes.c_int64),
        ("DisconnectTime", ctypes.c_int64),
        ("LastInputTime", ctypes.c_int64),
        ("CurrentTime", ctypes.c_int64),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    )


class _WtsInfoEx(ctypes.Structure):
    _fields_ = (("Level", wintypes.DWORD), ("Data", _WtsInfoLevel1))


class WindowsSessionReads:  # pragma: no cover - live, Windows only
    """`SessionReads` over wtsapi32 and user32. Any failure reads as None (a transition)."""

    def __init__(self) -> None:
        self._wts: Any = ctypes.WinDLL("wtsapi32", use_last_error=True)  # Any: a ctypes DLL
        self._u32: Any = ctypes.WinDLL("user32", use_last_error=True)  # Any: a ctypes DLL
        wts, u = self._wts, self._u32
        wts.WTSQuerySessionInformationW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
        )  # fmt: skip
        wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
        wts.WTSFreeMemory.argtypes = (ctypes.c_void_p,)
        wts.WTSFreeMemory.restype = None
        u.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        u.OpenInputDesktop.restype = wintypes.HANDLE
        u.GetUserObjectInformationW.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )  # fmt: skip
        u.GetUserObjectInformationW.restype = wintypes.BOOL
        u.CloseDesktop.argtypes = (wintypes.HANDLE,)
        u.CloseDesktop.restype = wintypes.BOOL

    def _info(self) -> _WtsInfoLevel1 | None:
        buffer, size = ctypes.c_void_p(), wintypes.DWORD()
        if not self._wts.WTSQuerySessionInformationW(
            WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTS_SESSION_INFO_EX,
            ctypes.byref(buffer), ctypes.byref(size),
        ):  # fmt: skip
            return None
        try:
            if size.value < ctypes.sizeof(_WtsInfoEx):
                return None
            info = ctypes.cast(buffer, ctypes.POINTER(_WtsInfoEx)).contents
            if info.Level != 1:
                return None
            copy = _WtsInfoLevel1()
            ctypes.pointer(copy)[0] = info.Data
            return copy
        finally:
            self._wts.WTSFreeMemory(buffer)

    def lock_state(self) -> int | None:
        info = self._info()
        if info is None or info.SessionFlags not in {0, 1}:
            return None  # unknown (0xFFFFFFFF) or unread: fail closed
        return int(info.SessionFlags)

    def connect_state(self) -> tuple[int, int, int] | None:
        info = self._info()
        if info is None:
            return None
        return int(info.SessionState), int(info.ConnectTime), int(info.DisconnectTime)

    def input_desktop(self) -> str | None:
        desktop = self._u32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not desktop:
            return None  # cannot open it: not the Default desktop (fail closed)
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            if not self._u32.GetUserObjectInformationW(desktop, UOI_NAME, name,
                                                       ctypes.sizeof(name),
                                                       ctypes.byref(needed)):  # fmt: skip
                return None
            return name.value
        finally:
            self._u32.CloseDesktop(desktop)
