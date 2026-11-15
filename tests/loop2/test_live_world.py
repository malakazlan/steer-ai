"""Adapter slice 7a: `LiveWorld`, one per task (packet §1, §2, rev 4.0).

Built over an injected `Platform` (the Windows side), so construction, the switch, the wiring of
v1's executors and the run header are checked off Windows. Construction fails closed: a task
that could not stop on the person's key, or would write names in clear, never starts."""

from __future__ import annotations

import dataclasses
import gc
import json
import re
import threading
from collections.abc import Callable
from typing import Any

import pytest

from steerai.action.dispositions import Delivery
from steerai.action.execution import UiaBindings
from steerai.action.executor import Executor
from steerai.action.options import UiaOptions
from steerai.action.physical import PhysicalAction, PhysicalExecutor
from steerai.errors import ErrorCode, SteerError
from steerai.loop2.act_guards import InputGuard, ToggleGuard
from steerai.loop2.controller import PROMPT_VERSION
from steerai.loop2.decide_port import Reply, TaskGenerations
from steerai.loop2.live_world import (
    LiveConfig,
    LiveWorld,
    UiaElements,
    WindowsPlatform,
    enter_mta,
)
from steerai.loop2.live_world import Platform as LivePlatform
from steerai.loop2.loop import Dispatch, PortInterrupted, TokenUsage
from steerai.loop2.runner import run_task
from steerai.orchestrator.trace import ListTrace
from steerai.world.model import Snapshot
from tests.action.fakes import (
    TYPE_IDS,
    UIA_E_ELEMENTNOTAVAILABLE,
    FakeHandle,
    FakeWindows,
    com_error,
)
from tests.action.test_physical import (
    TARGET_ID,
    FakeInput,
    _edit,
    fast_clock,  # noqa: F401 - the v1 fixture, used by name
)
from tests.action.test_physical import _snapshot as physical_snapshot
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
    PASSWORD,
    PRICE_READ_ON,
    PRICE_URL,
    act,
    snapshot,
    task,
)


class Monitor:
    def __init__(self) -> None:
        self.killed = False
        self.ok = True
        self.maintained = 0
        self.closed = 0
        self.last = 0.0
        self.raises: Exception | None = None
        """Raised by `maintain` and `healthy` (a hook thread that failed)."""

    def kill_requested(self) -> bool:
        return self.killed

    def healthy(self) -> bool:
        if self.raises is not None:
            raise self.raises
        return self.ok

    def maintain(self) -> None:
        self.maintained += 1
        if self.raises is not None:
            raise self.raises

    def last_user_input(self) -> float:
        return self.last

    def close(self) -> None:
        self.closed += 1


class Platform:
    """The Windows side, recorded; each knob fails one construction check."""

    def __init__(self) -> None:
        self.dpi = True
        self.uia_error: Exception | None = None
        self.monitor_error: Exception | None = None
        self.dpapi_error: Exception | None = None
        self.locked: bool | Exception | None = False
        self.desktop: str | Exception = "Default"
        self.monitor = Monitor()
        self.client = object()
        names = {v: k for k, v in TYPE_IDS.items()}
        self.bindings = UiaBindings(control_types=names, interfaces={})
        self.calls: list[str] = []
        self.com_apartment = "MTA"
        self.protected: list[bytes] = []
        self.flush_raises = False
        self.prompts: list[list[str]] = []
        self.click_named: str | None = None
        self.screen = snapshot(PAGE)
        self.fake_input = FakeInput()
        self.handles: list[FakeHandle] | None = None
        """When set, each read is the screen with these live handles, stamped on the clock."""

    def dpi_aware(self) -> bool:
        self.calls.append("dpi")
        return self.dpi

    def apartment(self) -> str:
        return self.com_apartment

    def uia(self) -> tuple[Any, UiaBindings]:
        self.calls.append("uia")
        if self.uia_error is not None:
            raise self.uia_error
        return self.client, self.bindings

    def start_monitor(self, kill_key: str, clock: Any) -> Monitor:  # Any: the world's clock
        self.calls.append(f"monitor:{kill_key}")
        self.monitor_clock = clock
        if self.monitor_error is not None:
            raise self.monitor_error
        return self.monitor

    def protect(self, data: bytes) -> bytes:
        self.calls.append("dpapi")
        if self.dpapi_error is not None:
            raise self.dpapi_error
        self.protected.append(data)
        return b"blob"

    def session_locked(self) -> bool | None:
        if isinstance(self.locked, Exception):
            raise self.locked
        return self.locked

    def input_desktop(self) -> str:
        if isinstance(self.desktop, Exception):
            raise self.desktop
        return self.desktop

    def observer(self, client: Any, clock: Any, sleep: Any) -> Any:  # Any: clock, sleep, reader
        del client
        self.observer_clock = clock
        self.observer_sleep = sleep

        def read(_hwnd: int) -> Snapshot:
            if self.handles is None:
                return self.screen
            return dataclasses.replace(self.screen, taken_at=clock(), handles=self.handles)

        return read

    def facts(self, client: Any) -> Any:  # Any: a FactReader
        del client
        return _Facts()

    def windows(self, pid: int) -> tuple[int, ...]:
        del pid
        return (16,)

    def focused(self, client: Any) -> Any:  # Any: one read of the focused element
        del client
        return lambda: (self.fake_input.focused, False)

    def inputs(self, client: Any) -> Any:  # Any: v1's InputPort
        del client
        return self.fake_input

    def window_state(self) -> Any:  # Any: v1's WindowState
        return FakeWindows()

    def provider(self) -> Any:  # Any: the decide port's Call
        return self.reply

    def reply(self, _system: list[str], blocks: list[str]) -> Reply:
        self.prompts.append(blocks)
        decision: dict[str, Any] = {"stop": "cannot"}
        if len(self.prompts) == 1 and self.click_named is not None:
            pattern = re.compile(rf'^\s*(\d+) Button "{self.click_named}"', re.M)
            found = pattern.search(blocks[2])
            assert found is not None, "the button is listed"
            decision = {
                "subgoals": [
                    {"id": "s1", "desc": "go", "params": {"text": "red shoes"}, "closes": "page"}
                ],
                "actions": [{"verb": "click", "target": int(found.group(1)), "subgoal": "s1"}],
            }
        return Reply(json.dumps(decision), "end_turn", TokenUsage(1, 1))


class _Facts:
    def document_url_in(self, _snapshot: Snapshot, _document_id: int) -> None:
        return None

    def address_bar(self, _hwnd: int) -> None:
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        return FIELD.runtime_id

    def page_facts(self, _hwnd: int) -> Any:  # Any: PageFacts
        raise OSError("not read")


class Clock:
    """One fake monotonic clock, far from wall time (packet §1 one-clock rule); `pause` moves it."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.now += seconds


def _config(**changes: Any) -> LiveConfig:
    clock = Clock()
    config = LiveConfig(
        task_id="t-1",
        hwnd=16,
        kill_key="ctrl+alt+k",
        trace_level="redacted",
        trace=ListTrace(),
        generations=TaskGenerations(),
        clock=clock,
        pause=clock.pause,
    )
    return dataclasses.replace(config, **changes)


def _world(platform: Platform | None = None, **changes: Any) -> tuple[LiveWorld, Platform]:
    plat = Platform() if platform is None else platform
    return LiveWorld(_config(**changes), plat), plat


# ---- construction fails closed (packet §1, principal 6) -----------------------------------------


def test_a_process_not_per_monitor_v2_aware_never_starts() -> None:
    platform = Platform()
    platform.dpi = False
    with pytest.raises(SteerError) as failed:
        _world(platform)
    assert failed.value.code is ErrorCode.PERCEPTION_DEGRADED
    assert failed.value.message == "dpi_context"
    assert not any(c.startswith("monitor") for c in platform.calls)


def test_uia_that_cannot_be_created_never_starts() -> None:
    platform = Platform()
    platform.uia_error = OSError("CoCreateInstance failed")
    with pytest.raises(SteerError) as failed:
        _world(platform)
    assert (failed.value.code, failed.value.message) == (
        ErrorCode.PERCEPTION_DEGRADED,
        "uia_unavailable",
    )


@pytest.mark.parametrize(
    "error",
    [
        SteerError(ErrorCode.GATE_BLOCKED, "monitor_unavailable: the hot key is taken"),
        OSError("SetWindowsHookExW failed"),
    ],
)
def test_a_monitor_or_hot_key_that_cannot_start_never_starts(error: Exception) -> None:
    platform = Platform()
    platform.monitor_error = error
    with pytest.raises(SteerError) as failed:
        _world(platform)
    assert failed.value.code is ErrorCode.GATE_BLOCKED
    assert failed.value.message.startswith("monitor_unavailable")


def test_a_debug_trace_without_dpapi_never_starts_and_the_monitor_is_closed() -> None:
    platform = Platform()
    platform.dpapi_error = OSError("CryptProtectData failed")
    with pytest.raises(SteerError) as failed:
        _world(platform, trace_level="debug")
    assert (failed.value.code, failed.value.message) == (
        ErrorCode.GATE_BLOCKED,
        "trace_needs_dpapi",
    )
    assert platform.monitor.closed == 1


def test_a_redacted_trace_never_needs_dpapi() -> None:
    platform = Platform()
    platform.dpapi_error = OSError("CryptProtectData failed")
    _world(platform)
    assert "dpapi" not in platform.calls


def test_the_checks_run_in_order_before_anything_else() -> None:
    _, platform = _world(trace_level="debug")
    assert platform.calls[:4] == ["dpi", "uia", "monitor:ctrl+alt+k", "dpapi"]


# ---- the task's own state ---------------------------------------------------------------------


def test_each_task_bumps_the_shared_generation() -> None:
    """Packet §2.9: a reply asked under an earlier task is never used by this one."""
    generations = TaskGenerations()
    before = generations.current()
    _world(generations=generations)
    assert generations.current() == before + 1
    _world(generations=generations)
    assert generations.current() == before + 2


def test_the_task_fence_comes_from_secrets_and_differs_per_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = _world()
    second, _ = _world()
    assert re.fullmatch(r"[0-9a-f]{32}", first.task_fence)
    assert first.task_fence != second.task_fence
    asked: list[int] = []

    def token_hex(n: int) -> str:
        asked.append(n)
        return "f" * 32

    monkeypatch.setattr("secrets.token_hex", token_hex)
    assert _world()[0].task_fence == "f" * 32
    assert asked == [16]


def test_a_late_error_sink_that_fails_never_raises_and_is_counted() -> None:
    """#346: the sink may run on the decide worker's thread; it never raises there."""

    class Broken(ListTrace):
        def write(self, _record: Any) -> None:
            raise OSError("disk full")

    world, _ = _world(trace=Broken())
    world.late_error("late_model_timeout")
    assert world.late_sink_failures == 1


def test_a_late_error_is_traced_as_its_token() -> None:
    trace = ListTrace()
    world, _ = _world(trace=trace)
    world.late_error("late_model_timeout")
    assert '"token": "late_model_timeout"' in trace.lines[-1]


# ---- the switch (packet §2.10) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "token"),
    [
        ({}, None),
        ({"killed": True}, "killed"),
        ({"ok": False}, "monitor_lost"),
        ({"locked": True}, "session_locked"),
        ({"locked": None}, "secure_desktop"),
        ({"locked": OSError("unreadable")}, "secure_desktop"),
        ({"desktop": "Winlogon"}, "secure_desktop"),
        ({"desktop": OSError("unreadable")}, "secure_desktop"),
        ({"killed": True, "ok": False, "locked": True}, "killed"),
        ({"ok": False, "locked": True}, "session_locked"),
        ({"ok": False, "desktop": "Winlogon"}, "secure_desktop"),
    ],
)
def test_interrupted_reads_each_signal_failing_closed(
    setup: dict[str, Any], token: str | None
) -> None:
    platform = Platform()
    for name in ("killed", "ok"):
        if name in setup:
            setattr(platform.monitor, name, setup[name])
    for name in ("locked", "desktop"):
        if name in setup:
            setattr(platform, name, setup[name])
    world, _ = _world(platform)
    assert world.interrupted() == token


def test_interrupted_keeps_the_monitor_maintained() -> None:
    world, platform = _world()
    world.interrupted()
    assert platform.monitor.maintained == 1


def test_close_closes_the_monitor_once() -> None:
    world, platform = _world()
    world.close()
    world.close()
    assert platform.monitor.closed == 1


# ---- v1's executors, wired (AP-31, AP-54) ----------------------------------------------------


def test_the_executors_admit_on_the_worlds_clock() -> None:
    """AP-31: v1's Admission age check runs on the adapter's one monotonic clock."""
    world, _ = _world()
    assert isinstance(world.semantic, Executor)
    assert isinstance(world.physical, PhysicalExecutor)
    assert world.semantic._admission.clock is world.clock
    assert world.physical._admission.clock is world.clock


def test_the_semantic_executor_reads_options_through_the_client() -> None:
    """AP-54: without an OptionSource, v1 refuses every `choose`."""
    world, platform = _world()
    options = world.semantic._options
    assert isinstance(options, UiaOptions)
    assert options._client is platform.client


def test_both_executors_stop_on_the_switch_and_are_guarded() -> None:
    world, _ = _world()
    assert world.semantic._blocker == world.interrupted
    assert world.physical._blocker == world.interrupted
    assert isinstance(world.semantic._uia, ToggleGuard)
    assert isinstance(world.physical._inputs, InputGuard)


# ---- the ports -----------------------------------------------------------------------------------


def test_write_ahead_is_closed_in_step_3() -> None:
    world, _ = _world()
    ports = world.ports(task())
    sent = Dispatch(act("click", target=1), (9, 3), "Button", "Go", "R0", None)
    with pytest.raises(SteerError) as refused:
        ports.write_ahead(sent)
    assert (refused.value.code, refused.value.message) == (
        ErrorCode.GATE_BLOCKED,
        "write_ahead_in_step_3",
    )


def test_observe_keeps_a_secure_field_secure_for_the_whole_task() -> None:
    """Packet §1: `SecureMemory` on every snapshot (a "Show password" box never unmasks it)."""
    world, platform = _world()
    ports = world.ports(task())
    ports.observe()
    unmasked = dataclasses.replace(PASSWORD, secure=False)
    platform.screen = snapshot([e if e.id != PASSWORD.id else unmasked for e in PAGE])
    seen = ports.observe()
    assert next(e for e in seen.snapshot.elements if e.id == PASSWORD.id).secure


def test_observe_reads_the_full_facts_and_the_clock_is_the_worlds() -> None:
    world, _ = _world()
    ports = world.ports(task())
    seen = ports.observe()
    assert seen.focus == FIELD.runtime_id
    assert seen.windows == (16,)
    assert ports.clock is world.clock


# ---- the run header ([V2.40] Q5) ---------------------------------------------------------------


def test_the_run_header_names_the_model_effort_prompt_trace_and_price() -> None:
    world, _ = _world()
    assert world.header(task()) == {
        "event": "run_header",
        "loop": "v2",
        "model": "claude-opus-5",
        "effort": "medium",
        "prompt_version": PROMPT_VERSION,
        "trace_level": "redacted",
        "price": "not configured",
    }


def test_a_configured_price_is_said_with_where_it_was_read() -> None:
    from steerai.loop2.task import Budgets, TokenPrice  # noqa: PLC0415 - one test

    price = TokenPrice(
        usd_per_input_token=1e-6,
        usd_per_output_token=2e-6,
        source=PRICE_URL,
        read_on=PRICE_READ_ON,
    )
    budgets = dataclasses.replace(task().budgets, price=price)
    assert isinstance(budgets, Budgets)
    world, _ = _world()
    header = world.header(dataclasses.replace(task(), budgets=budgets))
    assert header["price"] == "configured"
    assert header["price_source"] == PRICE_URL
    assert header["price_read_on"] == PRICE_READ_ON.isoformat()


def test_the_header_is_the_traces_first_record() -> None:
    trace = ListTrace()
    world, _ = _world(trace=trace)
    world.ports(task())
    assert '"event": "run_header"' in trace.lines[0]


# ---- the live element reads (packet §2.7) ------------------------------------------------------


class _Handle(FakeHandle):
    password: bool | None = False

    @property
    def CurrentIsPassword(self) -> bool:  # noqa: N802 - COM naming
        if self.password is None:
            raise com_error(UIA_E_ELEMENTNOTAVAILABLE)
        return self.password


def test_the_identity_read_is_role_name_and_is_password() -> None:
    platform = Platform()
    elements = UiaElements(platform.client, platform.bindings)
    handle = _Handle(control_type=TYPE_IDS["Edit"], name="Search")
    assert elements.identity(handle) == ("Edit", "Search", False)
    handle.password = True
    assert elements.identity(handle)[2] is True


def test_a_failed_identity_read_is_a_taxonomy_failure() -> None:
    platform = Platform()
    elements = UiaElements(platform.client, platform.bindings)
    handle = _Handle(control_type=TYPE_IDS["Edit"], name="Search")
    handle.password = None
    with pytest.raises(SteerError) as failed:
        elements.identity(handle)
    assert failed.value.code is ErrorCode.ELEMENT_NOT_FOUND


# ---- the real platform ------------------------------------------------------------------------


def test_the_windows_platform_is_a_platform() -> None:
    """mypy checks the assignment: the real side has every method the adapter calls."""
    platform: LivePlatform = WindowsPlatform()
    assert platform is not None


@pytest.mark.windows
def test_the_focused_facts_are_one_read_of_rid_and_is_password() -> None:
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only

    focus, secure = UiaClient().focused_facts()
    assert isinstance(focus, tuple)
    assert secure is None or isinstance(secure, bool)


# ---- #354: the guard's kill tests on the factory's own executors -------------------------------

CLICK = PhysicalAction(verb="click", element_id=1)


def _now(world: LiveWorld, shown: Snapshot) -> Snapshot:
    """A snapshot read now on the world's clock (one clock: an older stamp is `snapshot_stale`)."""
    return dataclasses.replace(shown, taken_at=world.clock())


def test_a_kill_before_a_click_moves_nothing(
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    world, platform = _world()
    platform.monitor.killed = True
    result = world.physical.execute(CLICK, _now(world, physical_snapshot(FakeHandle())))
    assert platform.fake_input.moves == []
    assert platform.fake_input.clicks == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.evidence["detail"] == "killed"


def test_a_kill_after_the_move_sends_no_click(
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    """v1's own blocker reads the switch before `Call.send` marks the click sent: a clean
    `not_delivered`, and the guard behind it (rev 3.10 (e))."""
    world, platform = _world()

    def latch(_x: int, _y: int) -> None:
        platform.monitor.killed = True

    platform.fake_input.after_move = latch
    result = world.physical.execute(CLICK, _now(world, physical_snapshot(FakeHandle())))
    assert len(platform.fake_input.moves) == 1
    assert platform.fake_input.clicks == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.GATE_BLOCKED


def test_a_kill_between_bursts_stops_the_typing(
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    world, platform = _world()
    handle = _edit()
    platform.fake_input.sink = handle

    def latch(_inputs: FakeInput, index: int) -> None:
        if index == 1:
            platform.monitor.killed = True

    platform.fake_input.after_burst = latch
    typed = PhysicalAction(verb="type", element_id=1, text="abcdefghijkl")
    with world.desk.armed(frozenset({TARGET_ID})):
        result = world.physical.execute(typed, _now(world, physical_snapshot(handle, role="Edit")))
    assert platform.fake_input.bursts == ["text:8"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN


def test_keys_outside_an_armed_act_never_reach_the_window(
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    world, platform = _world()
    handle = _edit()
    typed = PhysicalAction(verb="type", element_id=1, text="abc")
    result = world.physical.execute(typed, _now(world, physical_snapshot(handle, role="Edit")))
    assert platform.fake_input.bursts == []
    assert result.delivery is not Delivery.DELIVERED_VERIFIED


# ---- one task end to end on one far-from-wall clock (packet §1) --------------------------------


def test_one_task_runs_through_the_ports_on_one_clock() -> None:
    """Observe, stamp, the decide port, v1's admission and the watch, all on the world's fake
    monotonic clock far from wall time: the click is admitted (never `snapshot_stale`) and no
    observation is out of the clock's domain."""
    platform = Platform()
    handles: list[FakeHandle] = [
        _Handle(control_type=TYPE_IDS.get(e.role, TYPE_IDS["Button"]), name=e.name) for e in PAGE
    ]
    platform.handles = handles
    platform.click_named = GO.name
    world, _ = _world(platform)
    outcome = run_task(task(), world.ports(task()))
    world.close()
    go = handles[[e.id for e in PAGE].index(GO.id)]
    assert [d.action.verb for d in outcome.dispatched] == ["click"]
    assert go.invoked == 1
    assert outcome.reason not in {"clock_domain", "port_unstable_ui"}
    assert platform.observer_clock is world.clock


def test_the_desk_reads_the_persons_last_input_from_the_monitor() -> None:
    """Rev 3.10 (c): the idle rule's last input is the monitor's, on the same clock."""
    world, platform = _world()
    platform.monitor.last = 1_000_123.5
    assert world.desk.last_input() == 1_000_123.5


# ---- #355 review round -----------------------------------------------------------------------

SENTINEL = "zqsentinelname"


def test_a_debug_trace_goes_to_its_file_only_through_dpapi(tmp_path: Any) -> None:
    """Principal 1: at level `debug` the records are protected by the platform and written as
    base64 of what DPAPI returned; the name never reaches the file in clear."""
    platform = Platform()
    path = tmp_path / "trace.debug.jsonl.dpapi"
    world, _ = _world(platform, trace_level="debug", debug_path=path)
    world.physical._debug.record("aim", {"element": {"name": SENTINEL}})
    world.close()
    data = path.read_bytes()
    assert SENTINEL.encode() not in data
    assert data.splitlines() == [b"YmxvYg=="]  # base64 of the fake's b"blob"
    assert any(SENTINEL.encode() in p for p in platform.protected)


@pytest.mark.parametrize("where", ["locked", "desktop"])
def test_a_session_read_that_raises_anything_is_the_secure_desktop(where: str) -> None:
    """Principal 2: fail closed on any error, never out of `run_task`."""
    platform = Platform()
    setattr(platform, where, RuntimeError("COM gone"))
    world, _ = _world(platform)
    assert world.interrupted() == "secure_desktop"


def test_a_monitor_that_raises_is_lost() -> None:
    platform = Platform()
    platform.monitor.raises = RuntimeError("hook thread died")
    world, _ = _world(platform)
    assert world.interrupted() == "monitor_lost"


def test_the_monitor_runs_on_the_worlds_clock() -> None:
    """Principal 3: `last_user_input` feeds the idle rule, so it is read on the port's clock."""
    world, platform = _world()
    assert platform.monitor_clock is world.clock


def test_the_window_reader_sleeps_kill_aware_on_the_worlds_clock() -> None:
    """Principal 4: the reader's own stability loop polls on the world's clock and pauses
    through the switch, so a kill ends a read in progress."""
    world, platform = _world()
    assert platform.observer_clock is world.clock
    platform.monitor.killed = True
    with pytest.raises(PortInterrupted):
        platform.observer_sleep(0.05)


def test_close_always_closes_the_monitor_and_counts_a_failed_flush(tmp_path: Any) -> None:
    platform = Platform()
    world, _ = _world(platform, trace_level="debug", debug_path=tmp_path / "d.dpapi")
    world.physical._debug.record("aim", {"x": 1})
    platform.dpapi_error = OSError("DPAPI gone at task end")
    world.close()
    assert platform.monitor.closed == 1
    assert world.debug_failure == "unavailable"


def test_the_ports_are_built_once_per_task() -> None:
    trace = ListTrace()
    world, _ = _world(trace=trace)
    world.ports(task())
    with pytest.raises(RuntimeError, match="once"):
        world.ports(task())
    assert sum('"run_header"' in line for line in trace.lines) == 1


class _HeldTrace(ListTrace):
    """A trace whose first write blocks until released, counting writers inside at once."""

    def __init__(self) -> None:
        super().__init__()
        self.inside = 0
        self.most = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, record: Any) -> None:
        self.inside += 1
        self.most = max(self.most, self.inside)
        self.entered.set()
        self.release.wait(timeout=2.0)
        self.inside -= 1
        super().write(record)


def test_every_trace_write_holds_one_lock() -> None:
    """One lock for every clear-trace write: the decide worker's late error and the loop
    thread's reads never write at once."""
    trace = _HeldTrace()
    world, _ = _world(trace=trace)
    worker = threading.Thread(target=world.late_error, args=("late_model_timeout",))
    worker.start()
    assert trace.entered.wait(timeout=2.0)
    reader = threading.Thread(target=world.world._trace, args=("read_unread:x",))
    reader.start()
    reader.join(timeout=0.2)  # held at the lock while the worker writes
    trace.release.set()
    worker.join(timeout=2.0)
    reader.join(timeout=2.0)
    assert trace.most == 1
    assert len(trace.lines) == 2


def _on_a_fresh_thread(work: Callable[[], object]) -> object:
    """Run `work` on a new thread, as 7b runs a task on its own loop thread (packet §1). Every
    COM object it made is released on that thread before it ends: a comtypes object collected
    later on another thread crashes the interpreter in `Release` (seen: a segfault in GC)."""
    out: list[object] = []

    def run() -> None:
        try:
            out.append(work())
        except BaseException as exc:  # noqa: BLE001 - handed back to the test thread
            out.append(exc)
        finally:
            gc.collect()  # the thread's COM objects die here, on their own thread

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=30.0)
    if isinstance(out[0], BaseException):
        raise out[0]
    return out[0]


@pytest.mark.windows
def test_the_windows_platform_constructs_and_closes() -> None:
    """The real side, on its own loop thread: UIA in the MTA, the hook monitor with its hot key,
    then a clean close."""
    from steerai.probes.windows import list_top_windows  # noqa: PLC0415 - Windows only

    hwnd = list_top_windows()[0].hwnd

    def task() -> object:
        world = LiveWorld(_config(hwnd=hwnd, kill_key="ctrl+alt+shift+f9"), WindowsPlatform())
        try:
            return world.interrupted()
        finally:
            world.close()
            del world

    assert _on_a_fresh_thread(task) in {None, "session_locked", "secure_desktop"}


@pytest.mark.windows
def test_a_loop_threads_uia_client_is_in_the_mta() -> None:
    """The #355 CI failure's root cause: `uia_snapshot._create_client` imports comtypes before
    `load_uia_module` sets `sys.coinit_flags`, so comtypes initialises its importing thread as an
    STA (measured: a fresh interpreter reads MAINSTA after `UiaClient()`). The v2 platform enters
    the MTA itself before UIA is created, on the loop thread."""
    platform = WindowsPlatform()

    def build() -> object:
        client, bindings = platform.uia()
        del client, bindings
        return platform.apartment()

    assert _on_a_fresh_thread(build) == "MTA"


def test_the_value_digest_key_is_random_per_task() -> None:
    """The tester's M7e: 32 random bytes per task (a value digest is never comparable across
    tasks, nor guessable)."""
    first, _ = _world()
    second, _ = _world()
    assert len(first._key) == 32
    assert first._key != second._key


# ---- #355 round, the principal's second pass -------------------------------------------------


class _Unwritable(ListTrace):
    def write(self, _record: Any) -> None:
        raise OSError("disk full")


def test_a_trace_that_cannot_be_written_never_raises_and_is_counted() -> None:
    """LW-16: a clear-trace write failure on the loop thread (a read token, the header) is
    counted as `trace_unwritable`, never raised raw into the task."""
    world, _ = _world(trace=_Unwritable())
    world.ports(task())  # the header's write fails
    world.world._trace("read_unread:element_not_found")
    assert world.report()["trace_unwritable"] == 2


def test_the_report_has_every_counter() -> None:
    world, _ = _world()
    world.close()
    assert world.report() == {"trace_unwritable": 0, "late_sink_failures": 0, "debug_failure": None}


def test_the_world_is_a_context_manager_that_always_closes() -> None:
    """LW-06: any exception in the task still stops the hooks and the hot key."""
    platform = Platform()
    with pytest.raises(KeyError), LiveWorld(_config(), platform):
        raise KeyError("a bug in the task")
    assert platform.monitor.closed == 1


def test_a_uia_client_outside_the_mta_never_starts() -> None:
    """LW-12, packet §1 threads: UIA runs on an MTA thread."""
    platform = Platform()
    platform.com_apartment = "STA"
    with pytest.raises(SteerError) as failed:
        _world(platform)
    assert failed.value.code is ErrorCode.PERCEPTION_DEGRADED
    assert failed.value.message == "com_apartment"


def test_the_ports_run_only_on_the_thread_that_built_the_world() -> None:
    """LW-12: COM elements never cross threads (packet §1, a design choice)."""
    world, _ = _world()
    ports = world.ports(task())
    seen: list[BaseException] = []

    def elsewhere() -> None:
        try:
            ports.observe()
        except SteerError as exc:
            seen.append(exc)

    other = threading.Thread(target=elsewhere)
    other.start()
    other.join(timeout=5.0)
    assert len(seen) == 1
    assert isinstance(seen[0], SteerError)
    assert seen[0].message == "wrong_thread"


def test_a_lock_or_secure_desktop_is_latched_for_the_task() -> None:
    """LW-18: once the session is locked (or the desktop unread), the task stays stopped for that
    reason, even if the person unlocks before the loop reads the switch again."""
    platform = Platform()
    platform.locked = True
    world, _ = _world(platform)
    assert world.interrupted() == "session_locked"
    platform.locked = False
    assert world.interrupted() == "session_locked"


def test_the_kill_outranks_a_latched_lock() -> None:
    platform = Platform()
    platform.locked = True
    world, _ = _world(platform)
    world.interrupted()
    platform.monitor.killed = True
    assert world.interrupted() == "killed"


# ---- [V2.57] and the round's last survivors ------------------------------------------------------


def test_nothing_at_all_is_sent_while_the_session_is_locked(
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    """[V2.57] LW-17: locked stops with that reason and injects no input of any kind: no
    monitor probe, no cursor move, no click, no key."""
    platform = Platform()
    platform.locked = True
    world, _ = _world(platform)
    assert world.interrupted() == "session_locked"
    result = world.physical.execute(CLICK, _now(world, physical_snapshot(FakeHandle())))
    typed = PhysicalAction(verb="type", element_id=1, text="abc")
    with world.desk.armed(frozenset({TARGET_ID})):
        world.physical.execute(typed, _now(world, physical_snapshot(_edit(), role="Edit")))
    assert platform.monitor.maintained == 0  # the self-probe is input too
    assert platform.fake_input.moves == []
    assert platform.fake_input.clicks == []
    assert platform.fake_input.bursts == []
    assert result.delivery is Delivery.NOT_DELIVERED


def test_no_ports_after_close() -> None:
    world, _ = _world()
    world.close()
    with pytest.raises(RuntimeError, match="before close"):
        world.ports(task())


@pytest.mark.parametrize("call", ["act", "read_value", "read_state"])
def test_the_act_and_the_live_reads_refuse_another_thread(call: str) -> None:
    world, _ = _world()
    ports = world.ports(task())
    sent = Dispatch(act("click", target=1), GO.runtime_id, "Button", "Go", "R0", None)
    calls: dict[str, Callable[[], object]] = {
        "act": lambda: ports.act(sent),
        "read_value": lambda: ports.read_value(FIELD),
        "read_state": lambda: ports.read_state(GO, "on"),
    }
    seen: list[BaseException] = []

    def elsewhere() -> None:
        try:
            calls[call]()
        except SteerError as exc:
            seen.append(exc)

    other = threading.Thread(target=elsewhere)
    other.start()
    other.join(timeout=5.0)
    assert [getattr(e, "message", "") for e in seen] == ["wrong_thread"]


# ---- the MTA entry (the #355 CI root cause) ----------------------------------------------------


@pytest.mark.parametrize("result", [0, 1], ids=["S_OK", "S_FALSE"])
def test_a_thread_that_enters_the_mta_may_use_uia(result: int) -> None:
    flags: list[int] = []

    def co_initialize_ex(flag: int) -> int:
        flags.append(flag)
        return result

    enter_mta(co_initialize_ex)
    assert flags == [0]  # COINIT_MULTITHREADED


@pytest.mark.parametrize(
    "result", [0x80010106 - (1 << 32), 0x80004005 - (1 << 32)], ids=["CHANGED_MODE", "E_FAIL"]
)
def test_a_thread_already_in_an_sta_or_failing_never_uses_uia(result: int) -> None:
    """RPC_E_CHANGED_MODE: the thread is already an STA; any failure is never MTA (fail closed)."""
    with pytest.raises(SteerError) as refused:
        enter_mta(lambda _flag: result)
    assert refused.value.code is ErrorCode.PERCEPTION_DEGRADED
    assert refused.value.message == "com_apartment"


def test_the_apartment_refusal_is_never_renamed_uia_unavailable() -> None:
    platform = Platform()
    platform.uia_error = SteerError(ErrorCode.PERCEPTION_DEGRADED, "com_apartment")
    with pytest.raises(SteerError) as failed:
        _world(platform)
    assert failed.value.message == "com_apartment"
