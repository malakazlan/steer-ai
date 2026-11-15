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
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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
from steerai.loop2.decide_port import Call, DecideCalls, TaskGenerations
from steerai.loop2.listing import LISTING_CAP
from steerai.loop2.loop import ActResult, Dispatch, Ports
from steerai.loop2.prompt_inputs import make_decide
from steerai.loop2.task import Task
from steerai.loop2.world_ports import WorldPorts, WorldTimes, kill_sleep
from steerai.orchestrator.trace import Trace
from steerai.planner.claude_planner import DEFAULT_MODEL, EFFORT
from steerai.world.effects import Observed, ValueFact, poll_tier, top_document
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.observe2 import DocumentSince, FactReader, FirstTouches, read_observed
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
"""Protected once at construction at level `debug`: DPAPI must work before a name is kept."""


class Monitor(Protocol):
    """The takeover monitor as the v2 adapter reads it (`session.monitor.HookMonitor`)."""

    def kill_requested(self) -> bool: ...
    def healthy(self) -> bool: ...
    def maintain(self) -> None: ...
    def last_user_input(self) -> float: ...
    def takeover_token(self) -> str: ...
    def close(self) -> None: ...


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

    def start_monitor(self, kill_key: str, clock: Callable[[], float], extra_info: int) -> Monitor:
        """The hook monitor on the world's clock (its `last_user_input` feeds the idle rule),
        counting only input tagged `extra_info` as Steer's own."""
        ...

    def use_extra_info(self, tag: int) -> None:
        """[V2.57] LW-02: every input this process sends from now on carries `tag`."""
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
    def window_identity(self, hwnd: int) -> WindowIdentity | None:
        """LW-03: what `hwnd` is now; None when any part of it cannot be read."""
        ...

    def focused(  # Any: the UiaClient
        self, client: Any
    ) -> Callable[[], tuple[tuple[int, ...], bool | None]]: ...
    def inputs(self, client: Any) -> InputPort: ...  # Any: the UiaClient
    def window_state(self) -> WindowState: ...
    def provider(self) -> Call: ...


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
        self._init_state()
        if held == "abandoned":
            self._token("session_abandoned")  # a previous Steer died holding it
        try:
            debug = self._debug_sink()
            self._build(client, bindings, debug)
        except BaseException:
            self.close()
            raise

    def _take_session(self, platform: Platform, config: LiveConfig) -> tuple[str, Monitor]:
        """[V2.57] LW-02: the session, then the run's tag, then the monitor on it. Any failure
        after the session was taken releases it (#361 H12)."""
        held = platform.acquire_session()  # one Steer per session
        try:
            self._input_tag = _run_tag()
            platform.use_extra_info(self._input_tag)
            try:
                monitor = platform.start_monitor(config.kill_key, config.clock, self._input_tag)
            except SteerError:
                raise  # HookMonitor's own GATE_BLOCKED monitor_unavailable, with its reason
            except Exception as exc:  # any other start failure: no kill switch, no task
                raise SteerError(ErrorCode.GATE_BLOCKED, "monitor_unavailable") from exc
        except BaseException:
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
        guard = InputGuard(
            platform.inputs(client),
            interrupted=self.interrupted,
            focused=platform.focused(client),
            clock=config.clock,
            pause=config.pause,
            last_input=self._monitor.last_user_input,
            takeover_token=self._monitor.takeover_token,
        )
        self.desk = GuardDesk(
            guard,
            last_input=self._monitor.last_user_input,
            takeover_token=self._monitor.takeover_token,
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
            pause=config.pause,
            touches=FirstTouches(),
            times=config.times,
            live=UiaElements(client, bindings),
            key=self._key,
            trace=self._token,
        )

    # ---- the switch (packet §2.10) ---------------------------------------------------------------

    def interrupted(self) -> str | None:
        """The latched kill first; then the lock and the input desktop (latched for the task,
        each failing closed: unread is `secure_desktop`, LW-17/18: the lock is the cause, a
        probe lost behind it only a symptom); then the monitor's health, kept up by `maintain`.
        Never raises. The act port's idle rule owns `takeover` (rev 3.10 (b))."""
        monitor = self._monitor
        if monitor.kill_requested():
            return "killed"
        if self._latched is None:
            self._latched = self._session_signal()
        if self._latched is not None:
            return self._latched
        try:
            monitor.maintain()
            healthy = monitor.healthy()
        except Exception:  # noqa: BLE001 - a monitor that cannot answer is lost (fail closed)
            return "monitor_lost"
        return None if healthy else "monitor_lost"

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
        kill_sleep(seconds, interrupted=self.interrupted, pause=self._config.pause)

    def _write(self, record: Mapping[str, str]) -> None:
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
            pause=config.pause,
        )
        calls = DecideCalls(
            self._platform.provider(),
            interrupted=self.interrupted,
            generations=config.generations,
            late_error=self.late_error,
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
            decide=make_decide(calls, goal=task.goal, task_fence=self.task_fence),
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
        if self._closed:
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
                self._platform.release_session()


_COM_HOLDERS = ("world", "semantic", "physical", "desk", "_observe", "_facts", "_client", "_debug")
"""Rev 4.0c: the attributes that hold COM objects (directly, or through an executor, the guard
or the world ports); `close` drops them on the loop thread."""


def _run_tag() -> int:
    """[V2.57] LW-02: a random nonzero 32-bit input tag for this run, never v1's."""
    while True:
        tag = secrets.randbits(32)
        if tag not in {0, _V1_TAG}:
            return tag


_V1_TAG = 0x5354_4545
"""v1's input tag (`win_input.STEER_EXTRA_INFO`); a v2 run never uses it."""


def run_on_loop_thread[T](work: Callable[[], T], *, release: Callable[[], None]) -> T:
    """Run one task's `work` on its own loop thread (packet §1, rev 4.0c, rev 4.2).

    `work` builds and closes its `LiveWorld` and returns plain data only, never an object
    holding COM. When it ends, on that thread: a collection (a backstop for a COM object kept
    alive by a cycle), then `release`, the platform's one `CoUninitialize`, exactly once and
    never before the release step. An exception from `work` is re-raised here as itself, so a
    decide worker's `SystemExit`, raised on the loop thread, ends the process from the caller
    (#346 (2))."""
    result: list[T] = []
    failure: list[BaseException] = []

    def loop() -> None:
        try:
            result.append(work())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller as itself
            failure.append(exc)
        finally:
            gc.collect()
            release()

    thread = threading.Thread(target=loop, name="steer-v2-loop")
    thread.start()
    thread.join()
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

    def start_monitor(self, kill_key: str, clock: Callable[[], float], extra_info: int) -> Monitor:
        from steerai.session.monitor import HookMonitor  # noqa: PLC0415 - Windows hooks

        monitor = HookMonitor(kill_key=kill_key, monotonic=clock, extra_info=extra_info)
        monitor.start()
        return monitor

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
