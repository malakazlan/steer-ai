"""Adapter slice 7a: `LiveWorld`, the v2 loop's `Ports` over the live window, one per task
(packet §1, §2, rev 4.0 design, rev 3.10 contract).

`LiveWorld(config, platform)` builds everything a task needs from existing modules, over an
injected `Platform` (the Windows side; `WindowsPlatform` is the real one), so construction, the
switch, the wiring of v1's executors and the run header are tested off Windows.

Construction fails closed, so the task never starts, in this order (packet §1, principal 6):
1. the process is not Per-Monitor-V2 DPI aware: `PERCEPTION_DEGRADED` / `dpi_context`;
2. UIA cannot be created: `PERCEPTION_DEGRADED` / `uia_unavailable`;
3. the hook monitor or the kill hot key cannot start: `GATE_BLOCKED` / `monitor_unavailable`;
4. at trace level `debug` (names, the prompt), DPAPI cannot protect: `GATE_BLOCKED` /
   `trace_needs_dpapi`. A plaintext trace holds closed tokens and counts only.
A failure after the monitor started closes it.

Then: the shared `TaskGenerations` is bumped (a previous task's late reply is never used, §2.9);
the task fence is `secrets.token_hex(16)`; the value-digest key is 32 random bytes in memory only;
v1's executors are built on the one monotonic clock with `blocker = interrupted` (AP-31), the
semantic one over `ToggleGuard` and `UiaOptions(client)` (AP-54), the physical one over
`InputGuard` (rev 3.10 (d)).
"""

from __future__ import annotations

import gc
import math
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from steerai.action.execution import UiaBindings, WindowState, code_for
from steerai.action.executor import Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.options import UiaOptions
from steerai.action.physical import InputPort, PhysicalExecutor
from steerai.debug_trace import BufferedDebugSink, DebugSink, NullDebugSink
from steerai.errors import ErrorCode, SteerError
from steerai.loop2.act_guards import GuardDesk, InputGuard, ToggleGuard
from steerai.loop2.act_port import ActPort
from steerai.loop2.controller import PROMPT_VERSION
from steerai.loop2.decide_port import Call, Counter, DecideCalls, TaskGenerations
from steerai.loop2.listing import LISTING_CAP
from steerai.loop2.loop import ActResult, Dispatch, PortInterrupted, Ports
from steerai.loop2.prompt_inputs import make_audit, make_decide
from steerai.loop2.task import Task
from steerai.loop2.world_ports import KILL_POLL_S, WorldPorts, WorldTimes, kill_sleep
from steerai.orchestrator.loop import IDLE_S, WAIT_POLL_S
from steerai.orchestrator.trace import Trace
from steerai.planner.claude_planner import DEFAULT_MODEL, EFFORT
from steerai.session.monitor import probe_deadline_s
from steerai.world.cover import CoverFacts, cover_of
from steerai.world.effects import Observed, ValueFact, poll_tier, top_document
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.observe2 import (
    DocumentSince,
    FactReader,
    FirstTouches,
    document_nodes,
    read_observed,
)
from steerai.world.secure import SecureMemory

TraceLevel = Literal["redacted", "debug"]
"""`redacted`: a clear trace of closed tokens and counts only. `debug`: also the DPAPI debug
sink, which may hold names and titles; it never starts without DPAPI (packet §1 traces)."""
SESSION_MUTEX = "Local\\SteerV2Session"
"""[V2.57] LW-02: one Steer per Windows session (the `Local\\` namespace)."""
_WAIT_OBJECT_0 = 0x0
_WAIT_ABANDONED = 0x80
COINIT_MULTITHREADED = 0x0
"""`CoInitializeEx`'s MTA flag (objbase.h)."""
DPAPI_PROBE = b"steer-v2-dpapi-probe"
ShowCommand = Literal["minimize"]
NudgeKind = Literal["move1px", "minrestore"]
DEFAULT_NUDGE: NudgeKind = "move1px"
"""[V2.64] (5): the gentle nudge first; it worked on 10 of 10 normal and snapped windows in the
evidence run (2026-11-15, Chrome 157). A maximised window still gets `minrestore`."""
SW_SHOWNORMAL = 1
WPF_SETMINPOSITION = 0x0001
"""WINDOWPLACEMENT.flags: "This flag must be specified if the coordinates are set in the
ptMinPosition member" (learn.microsoft.com, ns-winuser-windowplacement, read 2026-11-15);
without it the set-back left ptMinPosition at the parking point (measured 2026-11-15)."""
RESTORE_TRIES = 2
"""#400 REVISE 1: a put-back that fails is tried once more; failing again is `restore_failed`."""
DOCUMENT_WAIT_S = 1.0
"""[V2.64] (1): how long the start waits after the raise for the page before the nudge.
Measured (degenerate distribution), floor 1.0 s: 24 starts on 2026-11-15 (12 cold, 12 loaded,
Chrome 157, no trigger) had the page at the first read after the raise in every one (p99 0.0
s); max(p99 + 0.5 s, 1.0 s), the floor from the diagnosis run in which a raised window turned
visible unaided in about 1 s. A late nudge costs a 1-px move."""
DOCUMENT_POLL_S = 0.25
NUDGE_PAUSE_S = 0.2
"""[V2.62] addendum: the pause between the minimise and the restore of the start's nudge (the
value the diagnosis used; one run per nudge, provisional)."""
START_IDLE_S = 10.0
"""[V2.62] how long the task start waits for the person to go idle (they have usually just
typed the command) before the window is brought in front; provisional, unmeasured. Past it
the task does not start (`person_active`)."""
"""Protected once at construction at level `debug`: DPAPI must work before a name is kept."""


class Monitor(Protocol):
    """The takeover monitor as the v2 adapter reads it (`session.monitor.HookMonitor`)."""

    def kill_requested(self) -> bool: ...
    def healthy(self) -> bool: ...
    def maintain(self) -> None: ...
    def last_user_input(self) -> float: ...
    def takeover_token(self) -> str: ...
    def probe_now(self) -> None: ...
    def probe_seen(self) -> bool:
        """[V2.57] LW-01: both hooks saw an own event since the last probe."""
        ...

    def latency(self) -> tuple[int, int | None, int | None]:
        """LW-01: (callbacks timed, p95 ms, p99 ms)."""
        ...

    def close(self) -> None: ...


class HelperWorldMonitor(Monitor, Protocol):
    """[V2.60]: the helper monitor (`session.helper_monitor.HelperMonitor`): a `Monitor`, plus the
    watchdog's session and desktop transitions (§5.6) and a wait on the helper (HH-08)."""

    def session_change(self) -> str | None: ...
    def wait(self, seconds: float) -> None: ...


class Platform(Protocol):
    """The Windows side of one task. A COM client and bindings are opaque here (Any): built,
    used and released on the loop thread (packet §1 threads)."""

    def dpi_aware(self) -> bool: ...
    def uia(self) -> tuple[Any, UiaBindings]: ...  # Any: the UiaClient
    def apartment(self) -> str:
        """The calling thread's COM apartment after `uia()`: `MTA`, `STA`, `MAINSTA` or `NA`."""
        ...

    def release_com(self) -> None:
        """Balance `uia()`'s `CoInitializeEx` on this thread: one `CoUninitialize`, once."""
        ...

    def start_monitor(
        self, kill_key: str, clock: Callable[[], float], extra_info: int, deadline_s: float
    ) -> Monitor:
        """The hook monitor on the world's clock (its `last_user_input` feeds the idle rule),
        counting only input tagged `extra_info` as Steer's own."""
        ...

    def start_helper_monitor(
        self,
        kill_key: str,
        clock: Callable[[], float],
        extra_info: int,
        deadline_s: float,
        hook_timeout_ms: int | None,
    ) -> HelperWorldMonitor:
        """[V2.60]: the hook helper and its monitor; a start failure is a refusal (K16)."""
        ...

    def use_extra_info(self, tag: int) -> None:
        """[V2.57] LW-02: every input this process sends from now on carries `tag`."""
        ...

    def restore_extra_info(self) -> None:
        """#361 B3: v1's tag back on the process's input, at close and on a failed start."""
        ...

    def acquire_session(self) -> str:
        """[V2.57] LW-02: the session-local single-instance mutex, before the monitor starts:
        `acquired`, or `abandoned` (a previous holder died; acquired too). Already held raises
        `GATE_BLOCKED` / `steer_already_running`."""
        ...

    def release_session(self) -> None: ...

    def protect(self, data: bytes) -> bytes: ...
    def session_locked(self) -> bool | None: ...
    def input_desktop(self) -> str: ...
    def observer(  # Any: the UiaClient
        self, client: Any, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> Callable[[int], Snapshot]:
        """One task's window reader, stamping `Snapshot.taken_at` on `clock` (packet §1); its own
        stability loop polls on `clock` and pauses through `sleep`, the kill-aware one."""
        ...

    def facts(self, client: Any) -> FactReader: ...  # Any: the UiaClient
    def windows(self, pid: int) -> tuple[int, ...]: ...
    def hook_timeout_ms(self) -> int | None:
        """LW-01: the effective LowLevelHooksTimeout in ms, or None when it is not set."""
        ...

    def window_identity(self, hwnd: int) -> WindowIdentity | None:
        """LW-03: what `hwnd` is now; None when any part of it cannot be read."""
        ...

    def window_cover(self, hwnd: int) -> CoverFacts:
        """[V2.62] one read of `hwnd`'s cover facts (`probes.windows.window_cover`)."""
        ...

    def window_placement(self, hwnd: int) -> WindowPlacement:
        """GetWindowPlacement; an OSError when it fails."""
        ...

    def set_window_placement(self, hwnd: int, placement: WindowPlacement) -> None:
        """SetWindowPlacement on the bound window only; an OSError when it fails."""
        ...

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """GetWindowRect; an OSError when it fails."""
        ...

    def set_window_rect(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
        """SetWindowPos to `rect`, no activation, no z-order change; the bound window only."""
        ...

    def on_primary_monitor(self, hwnd: int) -> bool:
        """Whether the window's nearest monitor is the primary one; an OSError when unread."""
        ...

    def show_window(self, hwnd: int, command: ShowCommand) -> None:
        """ShowWindow on the bound window only; an OSError when it is no window."""
        ...

    def focused(  # Any: the UiaClient
        self, client: Any
    ) -> Callable[[], tuple[tuple[int, ...], bool | None]]: ...
    def inputs(self, client: Any) -> InputPort: ...  # Any: the UiaClient
    def window_state(self) -> WindowState: ...
    def provider(self) -> Call: ...
    def counter(self) -> Counter: ...


@dataclass(frozen=True, slots=True)
class WindowPlacement:
    """[V2.64] (3) a window's placement (WINDOWPLACEMENT): its flags, its show state and its
    normal, minimised and maximised positions, saved before a nudge and set back after it with
    WPF_SETMINPOSITION added. A window never minimised before reads `min_pos` (-1, -1), unset:
    Windows fills in its parking point on that first minimise even with the flag (measured
    2026-11-15, 1 of 5; the other 4 byte-identical), so the put-back is compared on flags, show
    state, normal and maximised positions and the rect."""

    flags: int
    show_cmd: int
    normal: tuple[int, int, int, int]
    min_pos: tuple[int, int]
    max_pos: tuple[int, int]


@dataclass(frozen=True)
class WindowIdentity:
    """LW-03 ([V2.53] (1)): what the bound window is, read at binding: its process (pid and start
    time, so a reused pid is not the same process), the image, whether the handle is a top-level
    root (a reused handle can come back as a child window) and whether it is visible. The title,
    minimized and cloaked states change in normal use and are not part of it."""

    pid: int
    started: int
    image: str
    root: bool
    visible: bool


@dataclass(frozen=True)
class LiveConfig:
    task_id: str
    hwnd: int
    kill_key: str
    trace_level: TraceLevel
    trace: Trace
    """The clear trace: closed tokens, counts and the run header only."""
    generations: TaskGenerations
    """Shared by every task of the process: bumped when a task starts (§2.9)."""
    clock: Callable[[], float] = time.monotonic
    """The one monotonic clock (packet §1): the loop, the stamp, the watch, admission."""
    pause: Callable[[float], None] = time.sleep
    times: WorldTimes = field(default_factory=WorldTimes)
    debug_path: Path | None = None
    """Where the DPAPI debug file goes at level `debug`; none: no debug records are kept."""
    bound_pid: int | None = None
    """#361 H6: the pid of the window as it was listed; the binding's identity must match."""
    bound_image: str | None = None
    """#361 H6: the image of the listed window's process, lower case."""
    hook_helper: bool = False
    """[V2.60]: the hooks and the kill hot key in their own helper process (`HelperMonitor`)
    instead of a thread in this one. Off by default until the switch (packet rev 2b §10.1)."""
    gap_clock: Callable[[], float] = time.perf_counter
    """#383 blocker 3: the guard's re-read gap clock, QPC (`perf_counter` on CPython 3.12
    Windows), not the 15.625 ms world clock."""


def close_noting(close: Callable[[], None], cause: BaseException) -> None:
    """#385 HH4-06: a cleanup after a failure; a cleanup that raises is noted on `cause`, the
    error being raised, never put in its place."""
    try:
        close()
    except Exception as exc:  # noqa: BLE001 - noted on the cause, which is what propagates
        cause.add_note(f"the cleanup after this failure also failed: {exc!r}")


def bound_probe(
    tag: int,
    *,
    vk: int,
    nudge: Callable[..., None],
    key_up: Callable[..., None],
) -> Callable[[], None]:
    """LW-01: the helper's probe, the mouse nudge then the keyboard probe, bound to the run's
    tag (#385 HH4-11): never the process tag read at call time, so a late probe after the
    restore still goes out as this run's own input."""

    def probe() -> None:
        nudge(tag=tag)
        key_up(vk, tag=tag)

    return probe


def enter_mta(co_initialize_ex: Callable[[int], int]) -> None:
    """Put the calling thread in the COM MTA before any UIA object exists on it (packet §1).

    `CoInitializeEx(COINIT_MULTITHREADED)` returns S_OK (0) or S_FALSE (1, already in the MTA).
    Anything else is never the MTA: RPC_E_CHANGED_MODE means the thread is already an STA. That
    is refused (`com_apartment`), never worked around. Root cause of #355's CI failure: v1's
    `uia_snapshot._create_client` imports comtypes before `load_uia_module` sets
    `sys.coinit_flags`, so comtypes makes its importing thread an STA (a fresh interpreter reads
    MAINSTA after `UiaClient()`); the v2 loop thread enters the MTA itself first."""
    result = co_initialize_ex(COINIT_MULTITHREADED) & 0xFFFFFFFF
    if result not in (0, 1):
        raise SteerError(ErrorCode.PERCEPTION_DEGRADED, "com_apartment")


class UiaElements:
    """`world_ports.LiveElements` over UIA: v1's live reads plus the identity read (role, name,
    IsPassword) the world ports re-check before any read (packet §2.7)."""

    def __init__(self, client: Any, bindings: UiaBindings) -> None:  # Any: the UiaClient
        self._reads = UiaLiveReads(client, bindings)
        self._bindings = bindings

    def identity(self, handle: Any) -> tuple[str, str, bool | None]:  # Any: a COM element
        try:
            type_id = int(handle.CurrentControlType)
            name = str(handle.CurrentName or "")
            secure = bool(handle.CurrentIsPassword)
        except Exception as exc:  # a COMError has no stubs: mapped by its code, or re-raised
            code, _extra = code_for(exc)
            if code is None:
                raise
            raise SteerError(code, "identity_unread") from exc
        return self._bindings.control_types.get(type_id, str(type_id)), name, secure

    def value(self, handle: Any) -> str | None:  # Any: a COM element
        return self._reads.value(handle)

    def toggle_state(self, handle: Any) -> int | None:  # Any: a COM element
        return self._reads.toggle_state(handle)

    def is_selected(self, handle: Any) -> bool | None:  # Any: a COM element
        return self._reads.is_selected(handle)

    def expand_state(self, handle: Any) -> int | None:  # Any: a COM element
        return self._reads.expand_state(handle)


class LiveWorld:
    """One task's live world: build it (fail closed), take `ports(task)` once, `close()`."""

    def __init__(self, config: LiveConfig, platform: Platform) -> None:
        self._config = config
        self._platform = platform
        self.clock = config.clock
        self._helper_monitor: HelperWorldMonitor | None = None
        if not platform.dpi_aware():
            raise SteerError(ErrorCode.PERCEPTION_DEGRADED, "dpi_context")
        try:
            client, bindings = platform.uia()
        except SteerError:
            raise  # `com_apartment` from `enter_mta`, as it is
        except Exception as exc:  # creation fails as a COMError (no stubs) or OSError
            raise SteerError(ErrorCode.PERCEPTION_DEGRADED, "uia_unavailable") from exc
        if platform.apartment() != "MTA":  # LW-12, packet §1 threads
            del client, bindings
            raise SteerError(ErrorCode.PERCEPTION_DEGRADED, "com_apartment")
        self._thread = threading.get_ident()
        bound = platform.window_identity(config.hwnd)  # LW-03: the binding's identity
        if bound is None:
            del client, bindings
            raise SteerError(ErrorCode.APP_CRASHED, "window_unreadable")
        if (config.bound_pid is not None and bound.pid != config.bound_pid) or (
            config.bound_image is not None and bound.image != config.bound_image
        ):  # #361 H6: the handle now names another process than the one listed
            del client, bindings
            raise SteerError(ErrorCode.APP_CRASHED, "window_changed")
        self._bound = bound
        try:
            held, monitor = self._take_session(platform, config)
        except BaseException:
            del client, bindings  # released here, before the refusal leaves the loop thread
            raise
        self._monitor = monitor
        try:  # #362: everything after the monitor started closes the world on a failure
            self._init_state()
            if held == "abandoned":
                self._token("session_abandoned")  # a previous Steer died holding it
            debug = self._debug_sink()
            self._build(client, bindings, debug)
        except BaseException as exc:
            close_noting(self.close, exc)  # #385 HH4-06: never in place of the refusal
            raise

    def _take_session(self, platform: Platform, config: LiveConfig) -> tuple[str, Monitor]:
        """[V2.57] LW-02: the session, then the run's tag, then the monitor on it. Any failure
        after the session was taken releases it (#361 H12)."""
        # [V2.59]: the probe deadline from the system's hook timeout, read once at start
        self._hook_timeout_ms = platform.hook_timeout_ms()  # #385: the one read, passed on
        self._probe_deadline_s = probe_deadline_s(self._hook_timeout_ms)
        held = platform.acquire_session()  # one Steer per session
        try:
            self._input_tag = _run_tag()
            platform.use_extra_info(self._input_tag)
            try:
                monitor = self._start_monitor(platform, config)
            except SteerError:
                raise  # HookMonitor's own GATE_BLOCKED monitor_unavailable, with its reason
            except Exception as exc:  # any other start failure: no kill switch, no task
                raise SteerError(ErrorCode.GATE_BLOCKED, "monitor_unavailable") from exc
        except BaseException:
            try:
                platform.restore_extra_info()  # #361 B3: no run's tag outlives its start
            finally:
                platform.release_session()
            raise
        return held, monitor

    def _init_state(self) -> None:
        """The world's own state, set once the monitor runs."""
        self._closed = False
        self._ported = False
        self._trace_lock = threading.Lock()
        """One lock for every clear-trace write: the decide worker writes late errors."""
        self.debug_failure: str | None = None
        self.trace_unwritable = 0
        self.late_sink_failures = 0
        self._latched: str | None = None
        self._lost = False
        """[V2.57] LW-01: monitor_lost is final for the task: never re-installed and resumed."""
        self.monitor_lost = 0
        self.front_nudged: NudgeKind | None = None
        """[V2.64] the nudge the start applied, or None."""

    def _debug_sink(self) -> DebugSink:
        config = self._config
        if config.trace_level != "debug":
            return NullDebugSink()
        try:
            self._platform.protect(DPAPI_PROBE)
        except Exception as exc:  # DPAPI unreachable: names are never kept in clear
            raise SteerError(ErrorCode.GATE_BLOCKED, "trace_needs_dpapi") from exc
        if config.debug_path is None:
            return NullDebugSink()
        return BufferedDebugSink(config.debug_path, protect=self._platform.protect)

    def _build(self, client: Any, bindings: UiaBindings, debug: DebugSink) -> None:  # Any: COM
        config, platform = self._config, self._platform
        config.generations.bump()  # a reply asked under an earlier task is never used (§2.9)
        self.task_fence = secrets.token_hex(16)
        self._key = secrets.token_bytes(32)
        self._client = client
        self._observe = platform.observer(client, config.clock, self._kill_aware_sleep)
        self._facts = platform.facts(client)
        self._secure = SecureMemory()
        self._stamp = DocumentSince(clock=config.clock)
        self._inputs = platform.inputs(client)
        guard = InputGuard(
            self._inputs,
            interrupted=self.interrupted,
            focused=platform.focused(client),
            clock=config.clock,
            pause=self._pause,
            last_input=self._monitor.last_user_input,
            takeover_token=self._monitor.takeover_token,
            gap_clock=config.gap_clock,
        )
        self.desk = GuardDesk(
            guard,
            last_input=self._monitor.last_user_input,
            takeover_token=self._monitor.takeover_token,
            prove=self._prove_hooks,
        )
        self.semantic = Executor(
            uia=ToggleGuard(control_types=bindings.control_types, interfaces=bindings.interfaces),
            windows=platform.window_state(),
            clock=config.clock,
            blocker=self.interrupted,
            options=UiaOptions(client),
        )
        self.physical = PhysicalExecutor(
            uia=bindings,
            windows=platform.window_state(),
            clock=config.clock,
            inputs=guard,
            blocker=self.interrupted,
            debug=debug,
        )
        self._debug = debug
        self.world = WorldPorts(
            poll_read=self._poll_read,
            full_from=self._full_from,
            interrupted=self.interrupted,
            clock=config.clock,
            pause=self._pause,
            touches=FirstTouches(),
            times=config.times,
            live=UiaElements(client, bindings),
            key=self._key,
            trace=self._token,
            cover=self._cover,
        )

    # ---- [V2.62] the task start: the bound window in front -------------------------------------

    def bring_to_front(self, *, nudge: NudgeKind = DEFAULT_NUDGE) -> None:
        """Before the task ([V2.62], [V2.64]): wait, kill-aware and bounded by `START_IDLE_S`, for
        the person to be idle `IDLE_S` (v1's rule); bring the bound window in front through v1's
        `acquire_foreground` (a code action, never a model verb); check it is the foreground and
        uncovered; then wait up to `DOCUMENT_WAIT_S` for its page. Only a window still showing a
        browser frame with no Document while in front and uncovered is nudged, once (`_nudge`,
        the last resort), and its page waited for again; still missing, the start is refused
        (`page_hidden`); a window off the primary monitor is never nudged (unmeasured there) and
        is refused `page_hidden` at once. A refusal is a start refusal with its own reason. A
        switch token (a kill, the lock, a lost monitor) stops the start without sending anything
        more and is left to the task ([V2.59]). Mid-task Steer never raises or nudges the window
        (`InputGuard.acquire_foreground`). The CLI calls it once, before the task starts."""
        hwnd = self._config.hwnd
        if not self._idle_to_start():
            return
        try:
            self._inputs.acquire_foreground(hwnd)
        except OSError as exc:
            raise SteerError(ErrorCode.FOCUS_LOST, "foreground_refused") from exc
        if self.interrupted() is not None:
            return  # the lock or a kill during the acquire (SW-07's re-check): the task's own
        self._check_in_front(hwnd)
        needs = self._needs_nudge(hwnd)
        if not needs:
            return  # a page, a native window, or a switch token (None): nothing more here
        if not self._on_primary_monitor(hwnd):
            # #400 REVISE 3: a nudge on another monitor is unmeasured; never nudged there
            raise SteerError(ErrorCode.FOCUS_LOST, "page_hidden")
        self._nudge(hwnd, nudge)
        if self.interrupted() is not None:
            return  # a kill during the nudge: the placement is back, the task's own
        self._check_in_front(hwnd)
        if self._needs_nudge(hwnd):
            raise SteerError(ErrorCode.FOCUS_LOST, "page_hidden")

    def _check_in_front(self, hwnd: int) -> None:
        if self._inputs.foreground_hwnd() != hwnd:
            raise SteerError(ErrorCode.FOCUS_LOST, "not_in_front")
        if self._cover() is not None:
            raise SteerError(ErrorCode.FOCUS_LOST, "window_covered")

    def _needs_nudge(self, hwnd: int) -> bool | None:
        """[V2.64] (4) the trigger, a shape and never a name: a browser frame (its address bar
        reads) with no Document at all, after waiting up to `DOCUMENT_WAIT_S` on the world's
        clock. False on any Document (an empty one is a slow first paint, left to the task's
        first observation: #400 REVISE 2) or on a native window (no Document and no address
        bar, decided on the first read); None on a switch token."""
        clock = self._config.clock
        started = clock()
        first = True
        while True:
            if self.interrupted() is not None:
                return None
            inside = document_nodes(self._full_from(self._poll_read()).snapshot)
            if inside is not None:
                return False
            if first:
                first = False
                if inside is None and not self._address_bar_reads(hwnd):
                    return False
            if clock() - started >= DOCUMENT_WAIT_S:
                return self._address_bar_reads(hwnd)
            self._config.pause(DOCUMENT_POLL_S)

    def _address_bar_reads(self, hwnd: int) -> bool:
        try:
            return self._facts.address_bar(hwnd) is not None
        except OSError:
            return False

    def _nudge(self, hwnd: int, nudge: NudgeKind) -> None:
        """[V2.64] one nudge of our own bound window, the last resort. `move1px` (one pixel
        across and back to the rect saved before it, the default) applies to a window in its
        normal state; any other, and every window when `minrestore` is chosen, is minimised then
        put back with its placement (GetWindowPlacement before, SetWindowPlacement after, with
        WPF_SETMINPOSITION) and, for a normal window that the placement moved (a snapped window's
        placement holds its pre-snap rect), its saved rect. The putting back runs whatever
        happens after the first call, is tried `RESTORE_TRIES` times, and a put-back that still
        fails refuses the start `restore_failed` (never `nudge_refused`: the window may be left
        moved or minimised). A failed read or first call is `nudge_refused`. Recorded in
        `front_nudged`."""
        platform = self._platform
        try:
            placement = platform.window_placement(hwnd)
            rect = platform.window_rect(hwnd)
        except OSError as exc:
            raise SteerError(ErrorCode.FOCUS_LOST, "nudge_refused") from exc
        kind: NudgeKind = (
            "move1px"
            if nudge == "move1px" and placement.show_cmd == SW_SHOWNORMAL
            else "minrestore"
        )
        left, top, right, bottom = rect
        try:
            if kind == "move1px":
                platform.set_window_rect(hwnd, (left + 1, top, right + 1, bottom))
            else:
                platform.show_window(hwnd, "minimize")
        except OSError as exc:
            raise SteerError(ErrorCode.FOCUS_LOST, "nudge_refused") from exc
        try:
            self._config.pause(NUDGE_PAUSE_S)
        finally:
            if kind == "move1px":
                _restored(lambda: platform.set_window_rect(hwnd, rect))
            else:
                put_back = replace(placement, flags=placement.flags | WPF_SETMINPOSITION)
                _restored(lambda: platform.set_window_placement(hwnd, put_back))
        if kind == "minrestore" and placement.show_cmd == SW_SHOWNORMAL:
            _restored(lambda: self._rect_back(hwnd, rect))
        self.front_nudged = kind

    def _rect_back(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
        if self._platform.window_rect(hwnd) != rect:
            self._platform.set_window_rect(hwnd, rect)

    def _on_primary_monitor(self, hwnd: int) -> bool:
        """A monitor read that fails is not the primary monitor (fails closed: no nudge)."""
        try:
            return self._platform.on_primary_monitor(hwnd)
        except OSError:
            return False

    def _idle_to_start(self) -> bool:
        """True once the person is idle; False on a switch token (nothing sent); a person
        still active at `START_IDLE_S` refuses the start (`person_active`)."""
        clock = self._config.clock
        started = clock()
        while True:
            if self.interrupted() is not None:
                return False
            now = clock()
            if now - self._monitor.last_user_input() >= IDLE_S:
                return True
            if now - started >= START_IDLE_S:
                raise SteerError(ErrorCode.GATE_PENDING_USER, "person_active")
            try:
                self._kill_aware_sleep(min(WAIT_POLL_S, START_IDLE_S - (now - started)))
            except PortInterrupted:
                return False

    def _cover(self) -> str | None:
        """[V2.62] why the bound window is covered (`world.cover.cover_of`), or None; a read that
        fails is `cover_unread`, never "visible"."""
        try:
            facts = self._platform.window_cover(self._config.hwnd)
        except Exception:  # noqa: BLE001 - any failed read fails closed: a cover
            return "cover_unread"
        return cover_of(facts)

    # ---- the switch (packet §2.10) ---------------------------------------------------------------

    def interrupted(self) -> str | None:
        """The latched kill first; then the lock and the input desktop (latched for the task,
        each failing closed: unread is `secure_desktop`, LW-17/18: the lock is the cause, a
        probe lost behind it only a symptom); then the monitor's health, kept up by `maintain`.
        Never raises. The act port's idle rule owns `takeover` (rev 3.10 (b))."""
        killed = self._kill_token()
        if killed is not None:
            return killed
        monitor = self._monitor
        if self._latched is None:
            self._latched = self._session_signal() or self._helper_session_change()
        if self._latched is not None:
            return self._latched
        if self._lost:
            return "monitor_lost"
        try:
            monitor.maintain()
            healthy = monitor.healthy()
        except Exception:  # noqa: BLE001 - a monitor that cannot answer is lost (fail closed)
            healthy = False
        if healthy:
            return None
        self._lose()
        return "monitor_lost"

    def _kill_token(self) -> str | None:
        """The one kill read (#385 HH4-01): `killed`, or `monitor_lost` (latched) when the kill
        cannot be read (fail closed), else None. No kill-path read ever raises."""
        try:
            killed = self._monitor.kill_requested()
        except Exception:  # noqa: BLE001 - an unread kill is a lost monitor, latched and counted
            self._lose()
            return "monitor_lost"
        return "killed" if killed else None

    def _lose(self) -> None:
        """LW-01: latch monitor_lost for the task, counted once."""
        if not self._lost:
            self._lost = True
            self.monitor_lost += 1

    def _prove_hooks(self) -> None:
        """[V2.57] LW-01: just before each send, a fresh probe both hooks must see within the
        derived deadline ([V2.59]) on the world's clock, waited out kill-aware; else the monitor
        is lost and the send never happens."""
        monitor = self._monitor
        token = self._kill_token()  # #364 H3: the kill wins at every point
        if token is not None:
            raise PortInterrupted(token)
        if self._lost:  # #364 H1: a hook that answers again after a loss is not trusted
            raise PortInterrupted("monitor_lost")
        monitor.probe_now()
        started = self._config.clock()
        while True:
            token = self._kill_token()
            if token is not None:
                raise PortInterrupted(token)
            if not monitor.healthy():  # #364 H2, H5: errors, a dead thread, a failed send
                self._lose()
                raise PortInterrupted("monitor_lost")
            if monitor.probe_seen():
                return
            if self._config.clock() - started >= self._probe_deadline_s:
                token = self._kill_token()  # a kill after the last poll still wins
                if token is not None:
                    raise PortInterrupted(token)
                self._lose()
                raise PortInterrupted("monitor_lost")
            self._kill_aware_sleep(PROVE_POLL_S)

    def _session_signal(self) -> str | None:
        """The lock, then the input desktop; a read that fails (any error) or is unknown is the
        secure desktop (fail closed): the switch never raises out of the loop."""
        try:
            locked = self._platform.session_locked()
            desktop = None if locked is not False else self._platform.input_desktop()
        except Exception:  # noqa: BLE001 - unread is the secure desktop (fail closed)
            return "secure_desktop"
        if locked is True:
            return "session_locked"
        return None if desktop == "Default" else "secure_desktop"

    # ---- the reads -------------------------------------------------------------------------------

    def _on_loop_thread(self) -> None:
        """LW-12: COM elements are used only on the thread that built them (packet §1)."""
        if threading.get_ident() != self._thread:
            raise SteerError(ErrorCode.PERCEPTION_DEGRADED, "wrong_thread")

    def _poll_read(self) -> Observed:
        """The window's snapshot with the task's sticky secure flags (packet §1)."""
        self._on_loop_thread()
        try:
            snapshot = self._observe(self._config.hwnd)
        except (
            Exception
        ) as exc:  # re-raised: as `window_changed` if the window is not the bound one
            self._same_window(exc)
            raise
        self._same_window(None)
        return poll_tier(self._secure.apply(snapshot))

    def _same_window(self, cause: Exception | None) -> None:
        """LW-03: one identity read per snapshot read, so no snapshot of another window is ever
        used; changed or unreadable ends the task."""
        if self._platform.window_identity(self._config.hwnd) != self._bound:
            raise SteerError(ErrorCode.APP_CRASHED, "window_changed") from cause

    def _full_from(self, observed: Observed) -> Observed:
        """The full read's facts on a poll read's snapshot (packet §2.1)."""
        snapshot = observed.snapshot
        return read_observed(
            snapshot,
            self._facts,
            key=self._key,
            windows=lambda: self._platform.windows(snapshot.pid),
            page_facts=top_document(snapshot) is not None,
        )

    # ---- the trace -------------------------------------------------------------------------------

    def _kill_aware_sleep(self, seconds: float) -> None:
        helper = self._helper_monitor
        if helper is None:
            kill_sleep(seconds, interrupted=self.interrupted, pause=self._config.pause)
            return
        # HH-08: wait on the helper (its wake or its exit) in steps, re-reading the switch
        # between them, for the whole time asked on the world's clock
        deadline = self._config.clock() + (seconds if math.isfinite(seconds) else 0.0)
        while True:
            token = self.interrupted()
            if token is not None:
                raise PortInterrupted(token)
            left = deadline - self._config.clock()
            if left <= 0:
                return
            self._helper_step(helper, min(left, KILL_POLL_S))

    def _pause(self, seconds: float) -> None:
        """[V2.60] HH-08 for every hold (#385 principal 3: WorldPorts, the input guard, the act
        port): with the helper, a pause waits on it in kill-aware steps for its whole time on
        the world's clock, ending early only for a kill, which the caller's next switch read
        raises. Without it, the plain `config.pause`."""
        helper = self._helper_monitor
        if helper is None:
            self._config.pause(seconds)
            return
        clock = self._config.clock
        deadline = clock() + (seconds if math.isfinite(seconds) else 0.0)
        while self._kill_token() is None:
            left = deadline - clock()
            if left <= 0:
                return
            self._helper_step(helper, min(left, KILL_POLL_S))

    def _helper_step(self, helper: HelperWorldMonitor, step: float) -> None:
        """One kill-aware step on the helper (HH-08). A wait that returns early with no kill
        (a probe's wake, the helper's exit, a stuck event) sleeps the rest of the step plainly,
        so no wait spins (#385 HH4-03); a kill returns at once for the caller's switch read."""
        clock = self._config.clock
        started = clock()
        helper.wait(step)
        if self._kill_token() is not None:
            return
        rest = step - (clock() - started)
        if rest > 0:
            self._config.pause(rest)

    def _start_monitor(self, platform: Platform, config: LiveConfig) -> Monitor:
        """The in-process hook monitor, or with `hook_helper` the helper's ([V2.60])."""
        if not config.hook_helper:
            return platform.start_monitor(
                config.kill_key, config.clock, self._input_tag, self._probe_deadline_s
            )
        helper = platform.start_helper_monitor(
            config.kill_key,
            config.clock,
            self._input_tag,
            self._probe_deadline_s,
            self._hook_timeout_ms,
        )
        self._helper_monitor = helper
        return helper

    def _helper_session_change(self) -> str | None:
        """§5.6: a session or desktop transition the helper's watchdog saw during the task."""
        helper = self._helper_monitor
        return None if helper is None else helper.session_change()

    def _write(self, record: Mapping[str, Any]) -> None:  # Any: closed tokens and counts
        """One clear-trace line under the one lock (LW-24). A write that fails is counted as
        `trace_unwritable` and never raised raw into the task (LW-16)."""
        with self._trace_lock:
            try:
                self._config.trace.write(record)
            except Exception:  # noqa: BLE001 - a trace failure never stops a task; counted
                self.trace_unwritable += 1

    def _token(self, token: str) -> None:
        self._write({"event": "read", "token": token})

    def late_error(self, token: str) -> None:
        """#346: an abandoned call's failure, as its closed token. It may run on the decide
        worker's thread, so it never raises there: a sink that fails is counted, not re-raised."""
        with self._trace_lock:
            try:
                self._config.trace.write({"event": "late_error", "token": token})
            except Exception:  # noqa: BLE001 - the worker thread must never raise; counted
                self.late_sink_failures += 1

    def report(self) -> dict[str, int | str | None]:
        """The world's own counters for the run's final JSON (LW-26/27)."""
        return {
            "trace_unwritable": self.trace_unwritable,
            "late_sink_failures": self.late_sink_failures,
            "debug_failure": self.debug_failure,
            "monitor_lost": self.monitor_lost,
            "front_nudged": self.front_nudged,  # [V2.64] Three: the start's one nudge, or None
            **dict(zip(_LATENCY_KEYS, self._monitor.latency(), strict=True)),
        }

    def header(self, task: Task) -> Mapping[str, str]:
        """The run header ([V2.40] Q5): model, effort, prompt version, trace level, and the
        price as `configured` (with where and when it was read) or `not configured`."""
        price = task.budgets.price
        record = {
            "event": "run_header",
            "loop": "v2",
            "model": DEFAULT_MODEL,
            "effort": EFFORT,
            "prompt_version": PROMPT_VERSION,
            "trace_level": self._config.trace_level,
            "price": "not configured" if price is None else "configured",
            "listing_cap": str(LISTING_CAP),
            "always_keep_ceiling": str(2 * LISTING_CAP),
            "goal_word_subcap": str(LISTING_CAP // 3),
            "hook_timeout_ms": _shown(self._hook_timeout_ms),
            "probe_deadline_ms": str(round(self._probe_deadline_s * 1000)),
            "probe_key_ime": "closed_only",  # [V2.59]: verified with the IME closed only
            "hook_monitor": "helper" if self._config.hook_helper else "in_process",  # [V2.60]
        }
        if price is not None:
            record["price_source"] = price.source
            record["price_read_on"] = price.read_on.isoformat()
        return record

    # ---- the ports -------------------------------------------------------------------------------

    def ports(self, task: Task, *, header: Mapping[str, str] | None = None) -> Ports:
        """The task's `Ports`, built once; the header, with the caller's own fields (the CLI's
        limits), is the trace's first record, written once (#361 H13)."""
        if self._ported or self._closed:
            raise RuntimeError("a LiveWorld builds its ports once, for one task, before close")
        self._ported = True
        config = self._config
        clock = config.clock
        started = clock()
        self._write({**self.header(task), **(header or {})})
        act = ActPort(
            world=self.world,
            semantic=self.semantic,
            physical=self.physical,
            desk=self.desk,
            interrupted=self.interrupted,
            clock=clock,
            wall_left=lambda: task.budgets.max_wall_s - (clock() - started),
            pause=self._pause,
        )
        calls = DecideCalls(
            self._platform.provider(),
            interrupted=self.interrupted,
            generations=config.generations,
            late_error=self.late_error,
            counter=self._platform.counter(),
        )
        world = self.world
        check = self._on_loop_thread

        def act_here(dispatch: Dispatch) -> ActResult:
            check()
            self._same_window(None)  # #361 H7: the window may be replaced after the last read
            return act.act(dispatch)

        def read_value_here(element: Element) -> ValueFact | None:
            check()
            return world.read_value(element)

        def read_state_here(element: Element, wanted: str) -> bool | None:
            check()
            return world.read_state(element, wanted)

        return Ports(
            audit=make_audit(calls, goal=task.goal, task_fence=self.task_fence),
            decide=make_decide(
                calls,
                goal=task.goal,
                task_fence=self.task_fence,
                on_rejected=lambda shape: self._write({"event": "decision_rejected", **shape}),
            ),
            observe=world.observe,
            act=act_here,
            watch=world.watch,
            write_ahead=_write_ahead_closed,
            interrupted=self.interrupted,
            clock=clock,
            stamp=self._stamp,
            settle=world.settle,
            read_value=read_value_here,
            read_state=read_state_here,
            fact_of=world.fact_of,
        )

    def __enter__(self) -> LiveWorld:
        return self

    def __exit__(self, *_exc: object) -> None:
        """LW-06: any exception in the task still stops the hooks and the hot key."""
        self.close()

    def close(self) -> None:
        """Task end, on the loop thread only (rev 4.0c): the debug records are protected and
        written, every attribute holding a COM object is dropped (so they are released here, on
        their own thread, with no collection needed), and the monitor stops once."""
        if threading.get_ident() != getattr(self, "_thread", threading.get_ident()):
            raise SteerError(ErrorCode.PERCEPTION_DEGRADED, "wrong_thread")
        if getattr(self, "_closed", False):  # unset: `_init_state` itself failed
            return
        self._closed = True
        try:
            debug = getattr(self, "_debug", None)
            if debug is not None:
                self.debug_failure = debug.flush()  # its closed failure token, or None
        finally:
            for name in _COM_HOLDERS:
                if hasattr(self, name):
                    delattr(self, name)
            try:
                self._monitor.close()
            finally:
                try:
                    self._platform.restore_extra_info()  # #361 B3: v1's tag for the process
                finally:
                    self._platform.release_session()


_COM_HOLDERS = ("world", "semantic", "physical", "desk", "_observe", "_facts", "_client", "_debug")
"""Rev 4.0c: the attributes that hold COM objects (directly, or through an executor, the guard
or the world ports); `close` drops them on the loop thread."""


_LATENCY_KEYS = ("hook_events", "hook_latency_p95_ms", "hook_latency_p99_ms")
PROVE_POLL_S = 0.01
"""LW-01: how often the fresh probe's arrival is checked (unmeasured; the deadline decides)."""


def _shown(value: int | None) -> str:
    return "unset" if value is None else str(value)


def _run_tag() -> int:
    """[V2.57] LW-02: a random nonzero 32-bit input tag for this run, never v1's."""
    while True:
        tag = secrets.randbits(32)
        if tag not in {0, _V1_TAG}:
            return tag


_V1_TAG = 0x5354_4545
"""v1's input tag (`win_input.STEER_EXTRA_INFO`); a v2 run never uses it."""


def _without_frames(error: BaseException) -> BaseException:
    """#361 H4: the error and every error chained to it, without their tracebacks, so no frame
    of the task (nor a COM object it held) outlives the release. The caller re-raises it as
    itself; its own traceback then starts at the caller."""
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        link = pending.pop()
        if link is None or id(link) in seen:
            continue
        seen.add(id(link))
        link.__traceback__ = None
        pending += [link.__cause__, link.__context__]
    return error


def _restored(put_back: Callable[[], None]) -> None:
    """#400 REVISE 1: one put-back of the bound window, tried `RESTORE_TRIES` times; still
    failing, the start is refused `restore_failed`."""
    for attempt in range(RESTORE_TRIES):
        try:
            put_back()
        except OSError as exc:
            if attempt == RESTORE_TRIES - 1:
                raise SteerError(ErrorCode.FOCUS_LOST, "restore_failed") from exc
        else:
            return


def run_on_loop_thread[T](
    work: Callable[[], T],
    *,
    release: Callable[[], None],
    interrupted: Callable[[], None] | None = None,
) -> T:
    """Run one task's `work` on its own loop thread (packet §1, rev 4.0c, rev 4.2).

    `work` builds and closes its `LiveWorld` and returns plain data only, never an object
    holding COM. When it ends, on that thread: a collection (a backstop for a COM object kept
    alive by a cycle), then `release`, the platform's one `CoUninitialize`, exactly once and
    never before the release step. An exception from `work` is re-raised here as itself, so a
    decide worker's `SystemExit`, raised on the loop thread, ends the process from the caller
    (#346 (2)). A Ctrl+C while joining is heard (`interrupted`) and the join goes on: the kill
    hot key is the stop; the task's thread is never left running (#361 P2)."""
    result: list[T] = []
    failure: list[BaseException] = []

    def loop() -> None:
        try:
            result.append(work())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller as itself
            # #361 B2: the loop thread's frames as text (no frame, no COM object kept), so the
            # caller's traceback still shows where the task failed
            exc.add_note("".join(traceback.format_exception(exc)))
            failure.append(_without_frames(exc))
        finally:
            gc.collect()
            release()

    thread = threading.Thread(target=loop, name="steer-v2-loop")
    thread.start()
    while True:
        try:
            thread.join()
            break
        except KeyboardInterrupt:  # #361 P2: never abandon the thread that holds COM
            if interrupted is not None:
                interrupted()
    if failure:
        raise failure[0]
    return result[0]


def _write_ahead_closed(_dispatch: Dispatch) -> str:
    """Packet §2.6: closed in step 3; step 4c binds it to the DPAPI ledger."""
    raise SteerError(ErrorCode.GATE_BLOCKED, "write_ahead_in_step_3")


class WindowsPlatform:  # pragma: no cover - live, Windows only (the windows-marked tests)
    """`Platform` over Windows: UIA, the hook monitor, DPAPI and the Win32 reads. Every import is
    local, so the module loads off Windows and on the v1 path nothing of this runs."""

    def dpi_aware(self) -> bool:
        from steerai.probes.win_input import ensure_per_monitor_v2  # noqa: PLC0415 - Windows

        return ensure_per_monitor_v2()

    def uia(self) -> tuple[Any, UiaBindings]:  # Any: the UiaClient
        """UIA on this (the loop) thread, in the MTA: the thread enters the MTA first, and a
        comtypes not yet imported is told to initialise as MTA too (it reads the flag once)."""
        ole32 = _ole32()
        enter_mta(lambda flags: int(ole32.CoInitializeEx(None, flags)))
        self._entered = True
        if "comtypes" not in sys.modules:
            sys.coinit_flags = COINIT_MULTITHREADED  # type: ignore[attr-defined]  # comtypes reads it
        from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only

        return UiaClient(), UiaBindings.load()

    def use_extra_info(self, tag: int) -> None:
        from steerai.probes.win_input import use_extra_info  # noqa: PLC0415 - Windows only

        use_extra_info(tag)

    def restore_extra_info(self) -> None:
        from steerai.probes.win_input import STEER_EXTRA_INFO, use_extra_info  # noqa: PLC0415

        use_extra_info(STEER_EXTRA_INFO)

    def acquire_session(self) -> str:
        import ctypes  # noqa: PLC0415 - Windows only

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        handle = kernel32.CreateMutexW(None, 0, SESSION_MUTEX)
        if not handle:
            raise SteerError(ErrorCode.GATE_BLOCKED, "session_mutex_unavailable")
        waited = kernel32.WaitForSingleObject(handle, 0)
        if waited == _WAIT_OBJECT_0:
            self._session = handle
            return "acquired"
        if waited == _WAIT_ABANDONED:
            self._session = handle
            return "abandoned"
        kernel32.CloseHandle(handle)
        raise SteerError(ErrorCode.GATE_BLOCKED, "steer_already_running")

    def release_session(self) -> None:
        import ctypes  # noqa: PLC0415 - Windows only

        handle = getattr(self, "_session", None)
        if handle is None:
            return
        self._session = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)

    def release_com(self) -> None:
        if getattr(self, "_entered", False):
            self._entered = False
            _ole32().CoUninitialize()

    def start_monitor(
        self, kill_key: str, clock: Callable[[], float], extra_info: int, deadline_s: float
    ) -> Monitor:
        from steerai.session.monitor import PROBE_VK, HookMonitor  # noqa: PLC0415 - Windows hooks

        monitor = HookMonitor(
            kill_key=kill_key,
            monotonic=clock,
            extra_info=extra_info,
            per_hook=True,  # [V2.57] LW-01
            probe_vk=PROBE_VK,
            probe_deadline_s=deadline_s,  # [V2.59]
        )
        monitor.start()
        return monitor

    def start_helper_monitor(
        self,
        kill_key: str,
        clock: Callable[[], float],
        extra_info: int,
        deadline_s: float,
        hook_timeout_ms: int | None,
    ) -> HelperWorldMonitor:
        """[V2.60]: the hook helper in its job, then its monitor and watchdog; a failure closes
        the helper and refuses (K16)."""
        import os  # noqa: PLC0415 - Windows only

        from steerai.probes.win_input import key_up_probe, nudge_in_place  # noqa: PLC0415
        from steerai.session.helper_launch import (  # noqa: PLC0415 - Windows only
            HelperProcess,
            WindowsLaunch,
            kill_chord,
            this_interpreter,
        )
        from steerai.session.helper_monitor import (  # noqa: PLC0415 - Windows only
            HelperMonitor,
            WindowsSessionReads,
            end_this_process,
            qpc_clock,
        )
        from steerai.session.monitor import HOOK_TIMEOUT_CAP_MS, PROBE_VK  # noqa: PLC0415

        mods, vk = kill_chord(kill_key)
        interpreter, launcher = this_interpreter()
        helper = HelperProcess(
            WindowsLaunch(),
            interpreter=interpreter,
            launcher=launcher,
            system_root=os.environ["SYSTEMROOT"],
            run_tag=extra_info,
            probe_vk=PROBE_VK,
            kill_mods=mods,
            kill_vk=vk,
            hook_timeout_ms=HOOK_TIMEOUT_CAP_MS if hook_timeout_ms is None else hook_timeout_ms,
        )
        helper.start()

        probe = bound_probe(extra_info, vk=PROBE_VK, nudge=nudge_in_place, key_up=key_up_probe)
        try:
            qpc, freq = qpc_clock()
            return HelperMonitor(
                helper,
                session=WindowsSessionReads(),
                clock=clock,
                qpc=qpc,
                freq=freq,
                deadline_s=deadline_s,
                probe=probe,
                terminate_self=end_this_process,
            )
        except BaseException as exc:
            close_noting(helper.close, exc)
            raise

    def hook_timeout_ms(self) -> int | None:
        from steerai.session.monitor import read_hook_timeout_ms  # noqa: PLC0415 - Windows only

        return read_hook_timeout_ms()

    def protect(self, data: bytes) -> bytes:
        from steerai.debug_trace import protect_for_user  # noqa: PLC0415 - DPAPI

        return protect_for_user(data)

    def session_locked(self) -> bool | None:
        from steerai.probes.windows import session_locked  # noqa: PLC0415 - Windows only

        return session_locked()

    def input_desktop(self) -> str:
        from steerai.probes.windows import input_desktop_name  # noqa: PLC0415 - Windows only

        return input_desktop_name()

    def apartment(self) -> str:
        """`CoGetApartmentType` for this thread (APTTYPE: 0 STA, 1 MTA, 2 NA, 3 MAINSTA)."""
        import ctypes  # noqa: PLC0415 - Windows only

        kind, qualifier = ctypes.c_int(-1), ctypes.c_int(0)
        result = ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(kind), ctypes.byref(qualifier))
        if result != 0:
            return "unknown"
        return {0: "STA", 1: "MTA", 2: "NA", 3: "MAINSTA"}.get(kind.value, "unknown")

    def observer(  # Any: the UiaClient
        self, client: Any, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> Callable[[int], Snapshot]:
        """`WorldModel.observe` with its own defaults (`CAP_S` 1.5 s, packet §2.1)."""
        from steerai.world.model import WorldModel  # noqa: PLC0415 - Windows only

        model = WorldModel(client, clock=clock, sleep=sleep)
        return model.observe

    def facts(self, client: Any) -> FactReader:  # Any: the UiaClient
        return _ComFacts(client)

    def window_placement(self, hwnd: int) -> WindowPlacement:
        from steerai.probes.windows import get_placement  # noqa: PLC0415 - Windows only

        flags, show, normal, min_pos, max_pos = get_placement(hwnd)
        return WindowPlacement(
            flags=flags, show_cmd=show, normal=normal, min_pos=min_pos, max_pos=max_pos
        )

    def set_window_placement(self, hwnd: int, placement: WindowPlacement) -> None:
        from steerai.probes.windows import set_placement  # noqa: PLC0415 - Windows only

        set_placement(
            hwnd,
            flags=placement.flags,
            show_cmd=placement.show_cmd,
            normal=placement.normal,
            min_pos=placement.min_pos,
            max_pos=placement.max_pos,
        )

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        from steerai.probes.windows import get_window_rect  # noqa: PLC0415 - Windows only

        return get_window_rect(hwnd)

    def set_window_rect(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
        from steerai.probes.windows import set_window_rect  # noqa: PLC0415 - Windows only

        set_window_rect(hwnd, rect)

    def on_primary_monitor(self, hwnd: int) -> bool:
        from steerai.probes.windows import on_primary_monitor  # noqa: PLC0415 - Windows only

        return on_primary_monitor(hwnd)

    def show_window(self, hwnd: int, command: ShowCommand) -> None:
        import ctypes  # noqa: PLC0415 - Windows only

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # SW_MINIMIZE 6 (the restore is SetWindowPlacement); ShowWindow's return is the previous
        # visibility, never a failure, so only a dead handle is refused
        if not user32.IsWindow(hwnd):
            raise OSError("not_a_window")
        user32.ShowWindow(hwnd, {"minimize": 6}[command])

    def window_cover(self, hwnd: int) -> CoverFacts:
        from steerai.probes.windows import window_cover  # noqa: PLC0415 - Windows only

        return window_cover(hwnd)

    def window_identity(self, hwnd: int) -> WindowIdentity | None:
        from steerai.probes.windows import window_identity  # noqa: PLC0415 - Windows only

        facts = window_identity(hwnd)
        if facts is None:
            return None
        pid, started, image = facts["pid"], facts["start"], facts["image"]
        if not (isinstance(pid, int) and isinstance(started, int) and isinstance(image, str)):
            return None  # the process start time or its image could not be read
        if not image:
            return None
        return WindowIdentity(
            pid=pid,
            started=started,
            image=image,
            root=facts["root"] is True,
            visible=facts["visible"] is True,
        )

    def windows(self, pid: int) -> tuple[int, ...]:
        from steerai.probes.windows import list_top_windows  # noqa: PLC0415 - Windows only

        return tuple(sorted(w.hwnd for w in list_top_windows() if w.pid == pid))

    def focused(self, client: Any) -> Callable[[], tuple[tuple[int, ...], bool | None]]:
        return client.focused_facts  # type: ignore[no-any-return]  # UiaClient, rev 3.10 (a)

    def inputs(self, client: Any) -> InputPort:  # Any: the UiaClient
        from steerai.action.physical import LiveInput  # noqa: PLC0415 - Windows only

        return LiveInput(client)

    def window_state(self) -> WindowState:
        from steerai.action.execution import LiveWindows  # noqa: PLC0415 - Windows only

        return LiveWindows()

    def provider(self) -> Call:
        """[V2.40] Q5: v1's planner timeout and one SDK retry; the key comes from the SDK's own
        environment variables, never from this code."""
        import anthropic  # noqa: PLC0415 - the SDK, only when a real task runs

        from steerai.loop2.decide_port import anthropic_call  # noqa: PLC0415
        from steerai.planner.claude_planner import (  # noqa: PLC0415
            DEFAULT_MAX_RETRIES,
            DEFAULT_TIMEOUT_S,
        )

        client = anthropic.Anthropic(timeout=DEFAULT_TIMEOUT_S, max_retries=DEFAULT_MAX_RETRIES)
        return anthropic_call(client)

    def counter(self) -> Counter:
        """[V2.61]: count_tokens with no retry and the count's own timeout: a late count is cut
        by the bound, so a retry could only arrive after the wait has ended."""
        import anthropic  # noqa: PLC0415 - the SDK, only when a real task runs

        from steerai.loop2.decide_port import (  # noqa: PLC0415
            COUNT_TIMEOUT_S,
            anthropic_count,
        )

        return anthropic_count(anthropic.Anthropic(timeout=COUNT_TIMEOUT_S, max_retries=0))


def _ole32() -> Any:  # pragma: no cover - live, Windows only; Any: the ctypes DLL
    """ole32 with the two calls the loop thread makes typed (argtypes, restype)."""
    import ctypes  # noqa: PLC0415 - Windows only

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    return ole32


class _ComFacts:  # pragma: no cover - live, Windows only
    """`UiaClient` as a `FactReader` whose COM failures are all OSError, so `read_observed`
    reads them as not read (as the shadow probe's reader does)."""

    def __init__(self, client: Any) -> None:  # Any: the UiaClient
        self._client = client

    def document_url_in(self, snapshot: Snapshot, document_id: int) -> Any:  # Any: DocumentUrl
        return _com(lambda: self._client.document_url_in(snapshot, document_id))

    def address_bar(self, hwnd: int) -> str | None:
        return _com(lambda: self._client.address_bar(hwnd))  # type: ignore[no-any-return]

    def focused_runtime_id(self) -> tuple[int, ...]:
        return _com(self._client.focused_runtime_id)  # type: ignore[no-any-return]

    def page_facts(self, hwnd: int) -> Any:  # Any: PageFacts
        return _com(lambda: self._client.page_facts(hwnd))


def _com(read: Callable[[], Any]) -> Any:  # pragma: no cover - live; Any: the read's value
    from _ctypes import COMError  # noqa: PLC0415 - CPython on Windows

    try:
        return read()
    except COMError as exc:
        raise OSError("uia read failed") from exc
