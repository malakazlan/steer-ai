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
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from steerai.action.dispositions import Delivery
from steerai.action.execution import UiaBindings
from steerai.action.executor import Executor
from steerai.action.options import UiaOptions
from steerai.action.physical import PhysicalAction, PhysicalExecutor
from steerai.errors import ErrorCode, SteerError
from steerai.loop2 import controller
from steerai.loop2.act_guards import InputGuard, ToggleGuard
from steerai.loop2.controller import PROMPT_VERSION
from steerai.loop2.decide_port import Reply, TaskGenerations
from steerai.loop2.live_world import (
    LiveConfig,
    LiveWorld,
    UiaElements,
    WindowIdentity,
    WindowsPlatform,
    enter_mta,
    run_on_loop_thread,
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
        self.who = "takeover"
        self.probes = 0
        self.seen = True
        self.timed: tuple[int, int | None, int | None] = (0, None, None)
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

    def takeover_token(self) -> str:
        return self.who

    def probe_now(self) -> None:
        self.probes += 1

    def probe_seen(self) -> bool:
        return self.seen

    def latency(self) -> tuple[int, int | None, int | None]:
        return self.timed

    def close(self) -> None:
        self.closed += 1


class _Client:
    """A fake UIA client: weakly referable, so a test can see it released."""


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
        self.client: _Client | None = _Client()
        names = {v: k for k, v in TYPE_IDS.items()}
        self.bindings = UiaBindings(control_types=names, interfaces={})
        self.calls: list[str] = []
        self.session = "acquired"
        self.tag_error: Exception | None = None
        self.hook_timeout: int | None = None
        self.identity: WindowIdentity | None = WindowIdentity(
            pid=4242, started=133_000_000_000_000_000, image="browser.exe", root=True, visible=True
        )
        self.identity_reads = 0
        self.reads = 0
        self.screen_error: Exception | None = None
        self.com_apartment = "MTA"
        self.protected: list[bytes] = []
        self.flush_raises = False
        self.prompts: list[list[str]] = []
        self.counted = 0
        self.count_answer: int | None = None
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

    def release_com(self) -> None:
        self.calls.append("release_com")

    def restore_extra_info(self) -> None:
        self.calls.append("untag")

    def use_extra_info(self, tag: int) -> None:
        self.calls.append("tag")
        self.input_tag = tag
        if self.tag_error is not None:
            raise self.tag_error

    def acquire_session(self) -> str:
        self.calls.append("session")
        if self.session == "held":
            raise SteerError(ErrorCode.GATE_BLOCKED, "steer_already_running")
        return self.session

    def release_session(self) -> None:
        self.calls.append("release_session")

    def hook_timeout_ms(self) -> int | None:
        return self.hook_timeout

    def window_identity(self, hwnd: int) -> WindowIdentity | None:
        self.calls.append("identity")
        self.identity_reads += 1
        assert hwnd == 16
        return self.identity

    def uia(self) -> tuple[Any, UiaBindings]:
        self.calls.append("uia")
        if self.uia_error is not None:
            raise self.uia_error
        return self.client, self.bindings

    def start_monitor(  # Any: the world's clock
        self, kill_key: str, clock: Any, extra_info: int, deadline_s: float
    ) -> Monitor:
        self.calls.append(f"monitor:{kill_key}")
        self.monitor_clock = clock
        self.monitor_tag = extra_info
        self.monitor_deadline = deadline_s
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
            self.reads += 1
            if self.screen_error is not None:
                raise self.screen_error
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

    def counter(self) -> Any:  # Any: the decide port's Counter
        return self.count

    def count(self, _system: list[str], _blocks: list[str]) -> int | None:
        self.counted += 1
        return self.count_answer

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
    assert platform.calls.count("release_session") == 1  # [V2.57]: the session goes with it


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
    assert platform.calls[:7] == [
        "dpi", "uia", "identity", "session", "tag", "monitor:ctrl+alt+k", "dpapi",
    ]  # fmt: skip


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
    assert id(options._client) == id(platform.client)


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
        "listing_cap": "150",
        "always_keep_ceiling": "300",
        "goal_word_subcap": "50",
        "hook_timeout_ms": "unset",
        "probe_deadline_ms": "1050",
        "probe_key_ime": "closed_only",  # [V2.59]: an open IME is probed before stage 2
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


def test_the_platforms_exact_count_reaches_the_prompt_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[V2.61]: near the ceiling the guard asks the platform's counter; a count that fits sends
    the prompt, and the outcome says how many turns spent one."""
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", 1)
    monkeypatch.setattr(controller, "EXACT_BAND", 10.0**9)
    platform = Platform()
    platform.handles = [
        _Handle(control_type=TYPE_IDS.get(e.role, TYPE_IDS["Button"]), name=e.name) for e in PAGE
    ]
    platform.count_answer = 1
    world, _ = _world(platform)
    outcome = run_task(task(), world.ports(task()))
    world.close()
    assert platform.counted == len(platform.prompts) >= 1
    assert outcome.exact_counts == len(platform.prompts)


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


@pytest.mark.windows
def test_the_windows_platform_constructs_and_closes() -> None:
    """The real side, on its own loop thread: UIA in the MTA, the hook monitor with its hot key,
    then a clean close."""
    from steerai.probes.windows import list_top_windows  # noqa: PLC0415 - Windows only

    hwnd = list_top_windows()[0].hwnd
    platform = WindowsPlatform()

    def task() -> object:
        with LiveWorld(_config(hwnd=hwnd, kill_key="ctrl+alt+shift+f9"), platform) as world:
            return world.interrupted()

    done = run_on_loop_thread(task, release=platform.release_com)
    assert done in {None, "session_locked", "secure_desktop"}


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

    assert run_on_loop_thread(build, release=platform.release_com) == "MTA"


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
    assert world.report() == {
        "trace_unwritable": 0,
        "late_sink_failures": 0,
        "debug_failure": None,
        "monitor_lost": 0,
        "hook_events": 0,
        "hook_latency_p95_ms": None,
        "hook_latency_p99_ms": None,
    }


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


# ---- 7b: the loop thread and COM release (packet rev 4.0c, rev 4.2) ------------------------


def test_the_work_runs_on_its_own_loop_thread_and_hands_back_its_result() -> None:
    caller = threading.get_ident()
    seen: list[int] = []

    def work() -> str:
        seen.append(threading.get_ident())
        return "done"

    assert run_on_loop_thread(work, release=lambda: None) == "done"
    assert seen
    assert seen[0] != caller


def test_com_is_released_once_after_the_work_and_after_the_collection() -> None:
    order: list[str] = []

    def work() -> None:
        order.append("work")

    def release() -> None:
        order.append("release")

    run_on_loop_thread(work, release=release)
    assert order == ["work", "release"]


@pytest.mark.parametrize("error", [KeyError("bug"), SystemExit(3)], ids=["error", "exit"])
def test_the_loops_exception_reaches_the_caller_as_itself(error: BaseException) -> None:
    """#346 (2): a worker `SystemExit` raised on the loop thread ends the process from the
    caller, never only its own thread; COM is still released."""
    released: list[bool] = []

    def work() -> None:
        raise error

    with pytest.raises(type(error)) as raised:
        run_on_loop_thread(work, release=lambda: released.append(True))
    assert raised.value is error
    assert released == [True]


def test_close_releases_the_client_without_a_collection_while_the_world_lives() -> None:
    """Rev 4.0c: `close()` drops every COM-holding attribute; nothing but a cycle-free chain
    held the client, so it dies at once, on this thread."""
    platform = Platform()
    world, _ = _world(platform)
    client = platform.client
    assert client is not None
    ref = weakref.ref(client)
    platform.client = None
    del client
    gc.disable()
    try:
        world.close()
        assert ref() is None
        assert world.interrupted is not None  # the world itself is alive
    finally:
        gc.enable()


def test_close_refuses_any_thread_but_the_loop_thread() -> None:
    platform = Platform()
    world, _ = _world(platform)
    seen: list[BaseException] = []

    def elsewhere() -> None:
        try:
            world.close()
        except SteerError as exc:
            seen.append(exc)

    other = threading.Thread(target=elsewhere)
    other.start()
    other.join(timeout=5.0)
    assert [getattr(e, "message", "") for e in seen] == ["wrong_thread"]
    assert platform.monitor.closed == 0
    world.close()
    assert platform.monitor.closed == 1


# ---- [V2.57] LW-02: the session mutex and the run's own input tag ------------------------------


def test_the_session_is_taken_and_the_tag_set_before_the_monitor_starts() -> None:
    _, platform = _world(trace_level="debug")
    order = [c for c in platform.calls if c in {"session", "tag"} or c.startswith("monitor")]
    assert order == ["session", "tag", "monitor:ctrl+alt+k"]


def test_a_second_steer_never_starts() -> None:
    platform = Platform()
    platform.session = "held"
    with pytest.raises(SteerError) as refused:
        _world(platform)
    assert refused.value.message == "steer_already_running"
    assert not any(c.startswith("monitor") for c in platform.calls)


def test_an_abandoned_session_is_taken_and_traced() -> None:
    platform = Platform()
    platform.session = "abandoned"
    trace = ListTrace()
    _world(platform, trace=trace)
    assert any('"token": "session_abandoned"' in line for line in trace.lines)


def test_the_session_is_released_at_close_and_after_a_failed_start() -> None:
    world, platform = _world()
    world.close()
    assert platform.calls.count("release_session") == 1
    failing = Platform()
    failing.dpapi_error = OSError("no DPAPI")
    with pytest.raises(SteerError):
        _world(failing, trace_level="debug")
    assert failing.calls.count("release_session") == 1


def test_each_run_has_its_own_random_tag_used_by_the_monitor_and_the_input() -> None:
    first, a = _world()
    second, b = _world()
    assert a.input_tag == a.monitor_tag
    assert b.input_tag == b.monitor_tag
    assert a.input_tag != b.input_tag
    for tag in (a.input_tag, b.input_tag):
        assert 0 < tag < 1 << 32
        assert tag != 0x5354_4545  # never v1's
    del first, second


# ---- [V2.57] LW-05: the person's input between bursts is a takeover ----------------------------


def test_a_key_after_the_person_typed_during_the_act_is_a_takeover() -> None:
    from steerai.action.execution import RefusalError  # noqa: PLC0415 - this test only

    world, platform = _world()
    platform.fake_input.focused = FIELD.runtime_id
    with world.desk.armed(frozenset({FIELD.runtime_id})):
        world.physical._inputs.type_text("a")  # the guard itself
        platform.monitor.last = world.clock() + 1.0  # the person types after the act began
        with pytest.raises(RefusalError) as refused:
            world.physical._inputs.type_text("b")
    assert refused.value.detail == "takeover"
    assert platform.fake_input.bursts == ["text:1"]


def test_another_steers_key_during_the_act_is_other_steer_input() -> None:
    """[V2.58] (2): the guard refuses the same way, with the monitor's own token."""
    from steerai.action.execution import RefusalError  # noqa: PLC0415 - this test only

    world, platform = _world()
    platform.fake_input.focused = FIELD.runtime_id
    with world.desk.armed(frozenset({FIELD.runtime_id})):
        platform.monitor.last = world.clock() + 1.0
        platform.monitor.who = "other_steer_input"
        with pytest.raises(RefusalError) as refused:
            world.physical._inputs.type_text("b")
    assert refused.value.detail == "other_steer_input"
    assert world.desk.takeover_token() == "other_steer_input"


# ---- [V2.58] (3): only the process that starts the monitor takes the session -------------------


def test_the_chunked_runners_parent_never_takes_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.probes.test_suite_chunks import CHUNKS  # noqa: PLC0415 - its fakes
    from tests.probes.test_suite_chunks import World as Chunks  # noqa: PLC0415

    taken: list[str] = []
    monkeypatch.setattr(WindowsPlatform, "acquire_session", lambda _self: taken.append("x"))
    assert Chunks().run(CHUNKS, tmp_path) == 0
    assert taken == []


def test_the_session_is_taken_only_where_the_monitor_starts() -> None:
    """Every call of `acquire_session` in the package is in one function that, after it, starts
    the monitor: `LiveWorld._take_session`, which only `LiveWorld.__init__` calls. A chunk
    parent, a CLI or a helper never takes it."""
    import ast  # noqa: PLC0415 - this test only

    package = Path(__file__).resolve().parents[2] / "steerai"
    callers: list[str] = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            calls = [
                node.func.attr
                for node in ast.walk(function)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ]
            if "acquire_session" in calls:
                callers.append(f"{path.name}:{function.name}")
                assert calls.index("start_monitor") > calls.index("acquire_session")
    assert callers == ["live_world.py:_take_session"]
    source = (package / "loop2" / "live_world.py").read_text(encoding="utf-8")
    init = source[source.index("    def __init__(self, config: LiveConfig") :]
    init = init[: init.index(chr(10) + "    def ", 1)]
    assert source.count("self._take_session(") == init.count("self._take_session(") == 1


def test_a_random_draw_of_zero_or_v1s_tag_is_drawn_again(monkeypatch: pytest.MonkeyPatch) -> None:
    import secrets  # noqa: PLC0415 - this test only

    draws = iter([0x5354_4545, 0, 0x0BAD_CAFE])
    monkeypatch.setattr(secrets, "randbits", lambda _bits: next(draws))
    _, platform = _world()
    assert platform.input_tag == 0x0BAD_CAFE


def test_input_before_or_at_the_acts_start_is_not_a_takeover() -> None:
    """LW-05 counts only input after the act armed: the idle rule already judged what came
    before (here the person typed five seconds earlier, and once at the very instant)."""
    world, platform = _world()
    platform.fake_input.focused = FIELD.runtime_id
    platform.monitor.last = world.clock() - 5.0
    with world.desk.armed(frozenset({FIELD.runtime_id})):
        world.physical._inputs.type_text("a")
    platform.monitor.last = world.clock()
    with world.desk.armed(frozenset({FIELD.runtime_id})):
        world.physical._inputs.type_text("b")
    assert platform.fake_input.bursts == ["text:1", "text:1"]


# ---- LW-03 ([V2.53] (1)): the bound window's identity, re-checked on every observation ----------


def test_a_window_whose_identity_cannot_be_read_is_never_bound() -> None:
    platform = Platform()
    platform.identity = None
    with pytest.raises(SteerError) as refused:
        _world(platform)
    assert (refused.value.code, refused.value.message) == (
        ErrorCode.APP_CRASHED,
        "window_unreadable",
    )
    assert "session" not in platform.calls  # refused before the session is taken


def test_every_snapshot_read_re_checks_the_identity_read_at_binding() -> None:
    world, platform = _world()
    ports = world.ports(task())
    bound = platform.identity_reads
    ports.observe()
    ports.observe()
    assert platform.reads > 0
    assert platform.identity_reads == bound + platform.reads


@pytest.mark.parametrize(
    "field",
    [
        {"pid": 4243},
        {"started": 133_000_000_000_000_001},
        {"image": "other.exe"},
        {"root": False},
        {"visible": False},
    ],
    ids=["pid", "start", "image", "root", "visible"],
)
def test_a_changed_identity_ends_the_task(field: dict[str, object]) -> None:
    world, platform = _world()
    ports = world.ports(task())
    ports.observe()
    assert platform.identity is not None
    platform.identity = dataclasses.replace(platform.identity, **field)  # type: ignore[arg-type]  # one field per case
    with pytest.raises(SteerError) as ended:
        ports.observe()
    assert (ended.value.code, ended.value.message) == (ErrorCode.APP_CRASHED, "window_changed")


def test_an_identity_unreadable_mid_task_ends_the_task() -> None:
    world, platform = _world()
    ports = world.ports(task())
    platform.identity = None
    with pytest.raises(SteerError) as ended:
        ports.observe()
    assert (ended.value.code, ended.value.message) == (ErrorCode.APP_CRASHED, "window_changed")


def test_a_failed_read_of_a_replaced_window_is_window_changed() -> None:
    world, platform = _world()
    ports = world.ports(task())
    platform.screen_error = SteerError(ErrorCode.PERCEPTION_DEGRADED, "uia_timeout")
    assert platform.identity is not None
    platform.identity = dataclasses.replace(platform.identity, pid=1)
    with pytest.raises(SteerError) as ended:
        ports.observe()
    assert (ended.value.code, ended.value.message) == (ErrorCode.APP_CRASHED, "window_changed")


def test_a_failed_read_of_the_same_window_keeps_its_own_error() -> None:
    world, platform = _world()
    ports = world.ports(task())
    platform.screen_error = SteerError(ErrorCode.PERCEPTION_DEGRADED, "uia_timeout")
    with pytest.raises(SteerError) as failed:
        ports.observe()
    assert failed.value.message == "uia_timeout"


def test_the_runner_stops_on_a_changed_window() -> None:
    from steerai.loop2.runner import run_task  # noqa: PLC0415 - this test only

    world, platform = _world()
    ports = world.ports(task())
    assert platform.identity is not None
    platform.identity = dataclasses.replace(platform.identity, pid=1)
    outcome = run_task(task(), ports)
    assert (outcome.status, outcome.reason) == ("stopped", "port_app_crashed")


_FACTS: dict[str, object] = {
    "hwnd": 16, "pid": 4242, "start": 133_000_000_000_000_000, "image": "browser.exe",
    "title": "Shop", "visible": True, "root": True, "minimized": False, "cloaked": False,
}  # fmt: skip


@pytest.mark.parametrize(
    ("change", "readable"),
    [
        ({}, True),
        ({"title": "Other", "minimized": True, "cloaked": None}, True),
        ({"start": None}, False),
        ({"image": ""}, False),
        ({"pid": "4242"}, False),
    ],
    ids=["whole", "title_and_states_ignored", "start_unread", "image_unread", "pid_not_int"],
)
def test_the_windows_identity_is_v1s_facts_or_none_when_any_part_is_unread(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, object], readable: bool
) -> None:
    from steerai.probes import windows  # noqa: PLC0415 - v1's reader, replaced here

    monkeypatch.setattr(windows, "window_identity", lambda _hwnd: {**_FACTS, **change})
    expected = WindowIdentity(
        pid=4242, started=133_000_000_000_000_000, image="browser.exe", root=True, visible=True
    )
    assert WindowsPlatform().window_identity(16) == (expected if readable else None)


def test_the_windows_identity_of_a_gone_window_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from steerai.probes import windows  # noqa: PLC0415 - v1's reader, replaced here

    monkeypatch.setattr(windows, "window_identity", lambda _hwnd: None)
    assert WindowsPlatform().window_identity(16) is None


def test_the_windows_identity_carries_root_and_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reused handle that came back as a child window, or a hidden one, is a different
    identity from the bound visible root."""
    from steerai.probes import windows  # noqa: PLC0415 - v1's reader, replaced here

    monkeypatch.setattr(
        windows, "window_identity", lambda _hwnd: {**_FACTS, "root": False, "visible": False}
    )
    identity = WindowsPlatform().window_identity(16)
    assert identity is not None
    assert (identity.root, identity.visible) == (False, False)


def test_a_tag_that_cannot_be_set_releases_the_session() -> None:
    """#361 H12 (MTX-01): a failure between taking the session and starting the monitor."""
    platform = Platform()
    platform.tag_error = ValueError("an input tag is a nonzero 32-bit value")
    with pytest.raises(ValueError, match="tag"):
        _world(platform)
    assert platform.calls.count("release_session") == 1
    assert not any(c.startswith("monitor") for c in platform.calls)


@pytest.mark.parametrize(
    "listed", [{"bound_pid": 4243}, {"bound_image": "other.exe"}], ids=["pid", "image"]
)
def test_a_window_that_is_not_the_one_listed_is_never_bound(listed: dict[str, object]) -> None:
    """#361 H6 (WIN-01): the handle was reused between the listing and the binding."""
    platform = Platform()
    with pytest.raises(SteerError) as refused:
        _world(platform, **listed)
    assert (refused.value.code, refused.value.message) == (ErrorCode.APP_CRASHED, "window_changed")
    assert "session" not in platform.calls


def test_the_listed_window_binds() -> None:
    world, _ = _world(bound_pid=4242, bound_image="browser.exe")
    world.close()


@pytest.mark.parametrize("verb", ["click", "press"])
def test_a_window_replaced_after_the_last_read_gets_no_action(verb: str) -> None:
    """#361 H7 (WIN-02): the identity changes after the last poll and before the act; nothing
    reaches the window, through UIA or the keyboard: the act re-checks it first."""
    platform = Platform()
    handles: list[FakeHandle] = [
        _Handle(control_type=TYPE_IDS.get(e.role, TYPE_IDS["Button"]), name=e.name) for e in PAGE
    ]
    platform.handles = handles
    platform.fake_input.focused = FIELD.runtime_id
    world, _ = _world(platform)
    ports = world.ports(task())
    ports.observe()
    assert platform.identity is not None
    platform.identity = dataclasses.replace(platform.identity, pid=1)
    target = GO if verb == "click" else FIELD
    extra = {} if verb == "click" else {"key": "Enter"}
    sent = Dispatch(
        act(verb, target=target.id, **extra), target.runtime_id, target.role, target.name, "R0",
        None,
    )  # fmt: skip
    with pytest.raises(SteerError) as ended:
        ports.act(sent)
    assert (ended.value.code, ended.value.message) == (ErrorCode.APP_CRASHED, "window_changed")
    assert sum(h.invoked for h in handles) == 0
    assert platform.fake_input.bursts == []
    assert platform.fake_input.clicks == []


def test_a_failed_tasks_frames_are_gone_before_com_is_released() -> None:
    """#361 H4 (CLI-01): the failure's traceback, and a chained one, would keep `work`'s frame
    and its COM-holding locals alive past `CoUninitialize`."""
    refs: list[weakref.ref[_Client]] = []
    alive_at_release: list[bool] = []

    def work() -> None:
        held = _Client()
        refs.append(weakref.ref(held))
        try:
            raise OSError("a read failed")
        except OSError as cause:
            raise SteerError(ErrorCode.PERCEPTION_DEGRADED, "uia_timeout") from cause

    def release() -> None:
        alive_at_release.append(refs[0]() is not None)

    gc.disable()
    try:
        with pytest.raises(SteerError) as raised:
            run_on_loop_thread(work, release=release)
    finally:
        gc.enable()
    assert alive_at_release == [False]
    assert raised.value.message == "uia_timeout"


def _interrupt_the_first_join(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Ctrl+C reaches the main thread while it joins the loop thread (on 3.12 Windows it arrives
    once `join` returns, bpo-29971); the first join of `steer-v2-loop` raises it."""
    real = threading.Thread.join
    raised: list[int] = []

    def join(self: threading.Thread, timeout: float | None = None) -> None:
        if self.name == "steer-v2-loop" and not raised:
            raised.append(1)
            raise KeyboardInterrupt
        real(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", join)
    return raised


def test_ctrl_c_during_the_join_keeps_joining_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """#361 P2: the loop thread is never abandoned holding COM; the caller hears it."""
    raised = _interrupt_the_first_join(monkeypatch)
    heard: list[str] = []
    released: list[bool] = []
    result = run_on_loop_thread(
        lambda: "done", release=lambda: released.append(True), interrupted=lambda: heard.append("x")
    )
    assert (result, raised, heard, released) == ("done", [1], ["x"], [True])


class _RealTag(Platform):
    """The fake platform, but the process-wide input tag is the real one (`win_input`): a
    pure module global, no Windows call."""

    def use_extra_info(self, tag: int) -> None:
        from steerai.probes.win_input import use_extra_info  # noqa: PLC0415 - the real global

        super().use_extra_info(tag)
        use_extra_info(tag)

    def restore_extra_info(self) -> None:
        from steerai.probes.win_input import STEER_EXTRA_INFO, use_extra_info  # noqa: PLC0415

        super().restore_extra_info()
        use_extra_info(STEER_EXTRA_INFO)


def test_close_gives_the_process_back_v1s_tag() -> None:
    """#361 B3 (TAG-01): a run's random tag leaked into every later v1 record of the process
    (CI: 6 v1 tests read 1259532006 for 0x53544545)."""
    from steerai.probes.win_input import STEER_EXTRA_INFO, extra_info  # noqa: PLC0415

    platform = _RealTag()
    world, _ = _world(platform)
    assert extra_info() == platform.input_tag != STEER_EXTRA_INFO
    world.close()
    assert extra_info() == STEER_EXTRA_INFO


def test_a_failed_start_gives_the_process_back_v1s_tag() -> None:
    from steerai.probes.win_input import STEER_EXTRA_INFO, extra_info  # noqa: PLC0415

    platform = _RealTag()
    platform.monitor_error = OSError("SetWindowsHookExW failed")
    with pytest.raises(SteerError):
        _world(platform)
    assert extra_info() == STEER_EXTRA_INFO
    assert platform.calls.count("untag") == 1


# ---- [V2.57] LW-01: per-hook health in the v2 world ---------------------------------------------


def test_a_lost_monitor_stays_lost_and_is_counted_once() -> None:
    """monitor_lost ends the task in step 3: never re-installed and resumed, because a person
    may have typed while the hook was gone."""
    world, platform = _world()
    platform.monitor.ok = False
    assert world.interrupted() == "monitor_lost"
    platform.monitor.ok = True  # a later own event would make the core look healthy again
    assert world.interrupted() == "monitor_lost"
    assert world.report()["monitor_lost"] == 1


def test_the_desk_proves_both_hooks_with_a_fresh_probe() -> None:
    world, platform = _world()
    world.desk.prove_hooks()
    assert platform.monitor.probes == 1
    assert world.interrupted() is None


def test_a_probe_not_seen_by_both_hooks_in_time_is_a_lost_monitor() -> None:
    from steerai.session.monitor import probe_deadline_s  # noqa: PLC0415 - this test only

    platform = Platform()
    platform.hook_timeout = 300
    world, _ = _world(platform)
    platform.monitor.seen = False
    started = world.clock()
    with pytest.raises(PortInterrupted) as lost:
        world.desk.prove_hooks()
    assert [lost.value.token] == ["monitor_lost"]
    waited = world.clock() - started
    assert probe_deadline_s(300) <= waited < probe_deadline_s(300) + 0.05  # [V2.59]
    assert world.interrupted() == "monitor_lost"
    assert world.report()["monitor_lost"] == 1


def test_the_report_carries_the_callback_latency() -> None:
    world, platform = _world()
    platform.monitor.timed = (1200, 3, 16)
    report = world.report()
    keys = ("hook_events", "hook_latency_p95_ms", "hook_latency_p99_ms")
    assert tuple(report[k] for k in keys) == (1200, 3, 16)


@pytest.mark.parametrize(("timeout", "shown"), [(None, "unset"), (300, "300")])
def test_the_header_names_the_hook_timeout(timeout: int | None, shown: str) -> None:
    platform = Platform()
    platform.hook_timeout = timeout
    world, _ = _world(platform)
    assert world.header(task())["hook_timeout_ms"] == shown


def test_the_v2_platform_starts_a_per_hook_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real platform's monitor proves each hook with the probe key; nothing is started."""
    from steerai.session import monitor as monitor_module  # noqa: PLC0415 - this test only

    built: list[dict[str, object]] = []

    class Recorded:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

        def start(self) -> None:
            built.append({"started": True})

    monkeypatch.setattr(monitor_module, "HookMonitor", Recorded)
    WindowsPlatform().start_monitor("ctrl+alt+k", lambda: 0.0, 0x1234_5678, 1.05)
    assert built[0]["per_hook"] is True
    assert built[0]["probe_deadline_s"] == 1.05
    assert built[0]["probe_vk"] == monitor_module.PROBE_VK
    assert built[0]["extra_info"] == 0x1234_5678


def test_the_hook_timeout_is_read_from_the_registry_and_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Microsoft Learn, LowLevelKeyboardProc: HKCU\\Control Panel\\Desktop LowLevelHooksTimeout,
    in ms; Windows 10 1709 and later use 1000 for any value larger."""
    from steerai.session import monitor as hooks  # noqa: PLC0415 - this test only

    for raw, shown in (
        ("300", 300), (5000, 1000), (None, None), ("x", None),
        (chr(0xB2), None), (chr(0xFF13), None),  # #364 H7: isdigit() but no int() here
    ):  # fmt: skip
        monkeypatch.setattr(hooks, "_read_hooks_timeout", lambda raw=raw: raw)
        assert WindowsPlatform().hook_timeout_ms() == shown


# ---- #362: the close order, and a failure right after the monitor started ----------------------


def test_close_runs_in_its_order() -> None:
    """COM holders dropped first (on this thread), then the monitor stopped, then v1's tag
    back, then the session released: no hook outlives the session, no tag outlives the run."""
    platform = Platform()
    world, _ = _world(platform)
    seen: list[str] = []

    def monitor_close() -> None:
        seen.append("monitor" if not hasattr(world, "world") else "monitor_before_com")

    platform.monitor.close = monitor_close  # type: ignore[method-assign]  # a recording fake
    platform.calls.clear()
    world.close()
    assert seen == ["monitor"]
    assert platform.calls == ["untag", "release_session"]


@pytest.mark.parametrize("step", ["_init_state", "_token"])
def test_a_failure_right_after_the_monitor_started_closes_the_world(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """#362: `_init_state` and the abandoned-session token run inside the close-on-failure
    try, so the monitor, the tag and the session are all given back."""
    platform = Platform()
    platform.session = "abandoned"

    def broken(*_args: object) -> None:
        raise RuntimeError(step)

    monkeypatch.setattr(LiveWorld, step, broken)
    with pytest.raises(RuntimeError, match=step):
        _world(platform)
    assert platform.monitor.closed == 1
    assert platform.calls.count("untag") == 1
    assert platform.calls.count("release_session") == 1


def test_a_monitor_lost_twice_is_counted_once() -> None:
    world, platform = _world()
    platform.monitor.ok = False
    assert world.interrupted() == "monitor_lost"
    platform.monitor.seen = False
    with pytest.raises(PortInterrupted):
        world.desk.prove_hooks()
    assert world.report()["monitor_lost"] == 1


# ---- #364 H1/H2/H3/H5: the fresh proof's own guards --------------------------------------------


def test_after_monitor_lost_a_recovered_hook_proves_nothing() -> None:
    """H1 (LW-06): the latch is read first; a hook that answers again is not trusted."""
    world, platform = _world()
    platform.monitor.ok = False
    assert world.interrupted() == "monitor_lost"
    platform.monitor.ok = True
    with pytest.raises(PortInterrupted) as lost:
        world.desk.prove_hooks()
    assert [lost.value.token] == ["monitor_lost"]
    assert platform.monitor.probes == 0  # not even probed


def test_a_seen_probe_on_an_unhealthy_monitor_is_lost() -> None:
    """H2 (LW-31): callback errors, a dead thread or a failed loop count, not just the probe."""
    world, platform = _world()
    platform.monitor.ok = False  # e.g. a callback error; the probe itself arrived
    with pytest.raises(PortInterrupted) as lost:
        world.desk.prove_hooks()
    assert [lost.value.token] == ["monitor_lost"]
    assert world.report()["monitor_lost"] == 1


def test_a_probe_that_could_not_be_sent_is_lost_at_once() -> None:
    """H5: `on_probe_failed` makes the monitor unhealthy; no waiting out the deadline."""
    world, platform = _world()
    platform.monitor.seen = False

    def failed() -> None:
        platform.monitor.probes += 1
        platform.monitor.ok = False

    platform.monitor.probe_now = failed  # type: ignore[method-assign]  # a recording fake
    started = world.clock()
    with pytest.raises(PortInterrupted):
        world.desk.prove_hooks()
    assert world.clock() == started


@pytest.mark.parametrize("when", ["before", "after_the_last_poll"])
def test_a_kill_during_the_proof_wins(when: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """H3 (LW-05): killed, never monitor_lost, whenever the kill lands, even after the last
    poll and before the deadline check; nothing latched or counted as a loss."""
    from steerai.session.monitor import probe_deadline_s  # noqa: PLC0415 - this test only

    world, platform = _world()
    platform.monitor.seen = False
    if when == "before":
        platform.monitor.killed = True
    else:

        def last_poll(_seconds: float) -> None:  # the wait ends; then the kill lands
            world._config.pause(probe_deadline_s(None))
            platform.monitor.killed = True

        monkeypatch.setattr(world, "_kill_aware_sleep", last_poll)
    with pytest.raises(PortInterrupted) as stopped:
        world.desk.prove_hooks()
    assert [stopped.value.token] == ["killed"]
    assert world.report()["monitor_lost"] == 0


# ---- [V2.59]: the deadline in force, from the system --------------------------------------------


@pytest.mark.parametrize("timeout", [None, 300])
def test_the_monitor_and_the_header_get_the_derived_deadline(timeout: int | None) -> None:
    from steerai.session.monitor import probe_deadline_s  # noqa: PLC0415 - this test only

    platform = Platform()
    platform.hook_timeout = timeout
    world, _ = _world(platform)
    assert platform.monitor_deadline == probe_deadline_s(timeout)
    assert world.header(task())["probe_deadline_ms"] == str(round(probe_deadline_s(timeout) * 1000))
