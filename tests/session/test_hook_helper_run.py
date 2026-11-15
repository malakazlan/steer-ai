"""[V2.60] hook helper, PR 2: the helper's main, over a fake platform (no Win32, no hooks).

The helper has no stderr (its standard handles are not inherited; the coordinator accepted that
on one condition): every failure it can hit, before or after ready, is published in the status
words as `state = failed` plus an error code, never only as a traceback. Only a failure before the
status section is mapped cannot be published; it is a distinct exit code Steer reads instead."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

from steerai.session.helper_core import (
    EXIT_ARGS,
    EXIT_FAILED,
    EXIT_MAP,
    EXIT_OK,
    HelperHandles,
    helper_argv,
    parse_helper_argv,
    write_config,
)
from steerai.session.hook_classifier import LLKHF_UP
from steerai.session.hook_helper import HelperStepError, run_helper
from steerai.session.hook_layout import (
    ERROR_HOOKS,
    ERROR_HOTKEY,
    ERROR_INTERNAL,
    ERROR_LAYOUT,
    ERROR_LOOP,
    ERROR_MAP,
    LAYOUT_VERSION,
    STATE_CLOSED,
    STATE_FAILED,
    STATE_READY,
    STATE_STARTING,
    Words,
    config_size,
    config_words,
    status_size,
    status_words,
)

HANDLES = HelperHandles(config=11, status=12, quit=13, wake=14, parent=15)
RUN_TAG = 0x1234_5678
PROBE = 0x0E


def _argv() -> list[str]:
    return helper_argv(HANDLES, LAYOUT_VERSION)[3:]  # what the helper's main receives


@dataclass
class FakePlatform:
    """Each step can be made to fail; the waits are scripted; every call is recorded."""

    fail: str | None = None
    waits: list[str] = field(default_factory=lambda: ["timeout", "quit"])
    events: list[str] = field(default_factory=list)
    config_buf: bytearray = field(default_factory=lambda: bytearray(config_size()))
    status_buf: bytearray = field(default_factory=lambda: bytearray(status_size()))
    calls: list[str] = field(default_factory=list)
    keyboard: Callable[..., None] | None = None
    sinks: tuple[Callable[..., None], Callable[[], None]] | None = None
    chord: tuple[int, int] | None = None
    waited: list[tuple[int, int, int]] = field(default_factory=list)
    clock: int = 1_000_000

    def __post_init__(self) -> None:
        write_config(
            config_words(memoryview(self.config_buf)),
            run_tag=RUN_TAG,
            probe_vk=PROBE,
            kill_mods=0x7,
            kill_vk=0x53,
            dead_band_px=4,
            heartbeat_ms=100,
            hook_timeout_ms=1000,
            nonce=(1, 2),
        )

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise HelperStepError(name)

    def map_status(self, handle: int) -> memoryview:
        assert handle == HANDLES.status
        if self.fail == "map_status":
            raise OSError("map")
        return memoryview(self.status_buf)

    def map_config(self, handle: int) -> memoryview:
        assert handle == HANDLES.config
        self._step("map_config")
        return memoryview(self.config_buf).toreadonly()

    def qpc(self) -> int:
        if self.fail == "qpc":
            raise RuntimeError("an unexpected failure")
        self.clock += 1_000
        return self.clock

    def freq(self) -> int:
        return 10_000_000

    def tick(self) -> int:
        return self.clock // 10_000

    def unbiased(self) -> int:
        return self.clock

    def pid(self) -> int:
        return 4242

    def set_ws_floor(self) -> bool:
        self.calls.append("ws_floor")
        return True

    def settle_gc(self) -> None:
        self.calls.append("gc")

    def install_hooks(
        self,
        keyboard: Callable[..., None],
        mouse: Callable[..., None],
        on_error: Callable[[], None],
    ) -> None:
        self._step("install_hooks")
        self.keyboard = keyboard
        self.sinks = (mouse, on_error)

    def register_hotkey(self, mods: int, vk: int) -> None:
        self._step("register_hotkey")
        self.chord = (mods, vk)

    def wait(self, timeout_ms: int, parent: int, quit_event: int) -> str:
        self._step("wait")
        self.waited.append((timeout_ms, parent, quit_event))
        return self.waits.pop(0)

    def pump(self) -> Iterator[str]:
        self._step("pump")
        yield from self.events
        self.events = []

    def wake(self, handle: int) -> None:
        assert handle == HANDLES.wake
        self.calls.append("wake")

    def release(self) -> None:
        self.calls.append("release")


def _status(p: FakePlatform) -> Words:
    return status_words(memoryview(p.status_buf))


def test_a_clean_run_is_ready_then_closed() -> None:
    p = FakePlatform()
    assert run_helper(_argv(), p) == EXIT_OK
    s = _status(p)
    assert s.get("state") == STATE_CLOSED
    assert s.get("pid") == 4242
    assert s.get("heartbeat_count") == 1
    assert p.calls.index("install_hooks") < p.calls.index("register_hotkey")
    assert p.calls[-1] == "release"
    assert p.chord == (0x7, 0x53)  # the kill chord from the config
    assert p.waited[0] == (100, HANDLES.parent, HANDLES.quit)  # the heartbeat period
    assert p.sinks is not None


def test_ready_is_published_and_steer_woken_before_the_loop() -> None:
    p = FakePlatform(waits=["quit"])
    seen: list[int] = []
    original = p.wait

    def wait(timeout_ms: int, parent: int, quit_event: int) -> str:
        seen.append(_status(p).get("state"))
        return original(timeout_ms, parent, quit_event)

    p.wait = wait  # type: ignore[method-assign]  # a spy on the first wait
    run_helper(_argv(), p)
    assert seen == [STATE_READY]
    assert "wake" in p.calls[: p.calls.index("wait")]


@pytest.mark.parametrize(
    ("step", "code"),
    [
        ("map_config", ERROR_MAP),
        ("install_hooks", ERROR_HOOKS),
        ("register_hotkey", ERROR_HOTKEY),
        ("wait", ERROR_LOOP),
        ("pump", ERROR_LOOP),
        ("qpc", ERROR_INTERNAL),
    ],
)
def test_a_failure_in_each_step_is_published_as_failed_with_its_code(step: str, code: int) -> None:
    """The condition on the no-stderr deviation: never only a traceback."""
    p = FakePlatform(fail=step)
    assert run_helper(_argv(), p) == EXIT_FAILED
    s = _status(p)
    assert (s.get("state"), s.get("error")) == (STATE_FAILED, code)
    assert p.calls[-1] == "release"  # hooks and the hot key released on every path


def test_a_changed_config_is_published_as_a_layout_failure_and_nothing_is_hooked() -> None:
    p = FakePlatform()
    config_words(memoryview(p.config_buf)).set("run_tag", 0)
    assert run_helper(_argv(), p) == EXIT_FAILED
    s = _status(p)
    assert (s.get("state"), s.get("error")) == (STATE_FAILED, ERROR_LAYOUT)
    assert "install_hooks" not in p.calls


def test_a_status_section_that_cannot_be_mapped_is_its_own_exit_code() -> None:
    p = FakePlatform(fail="map_status")
    assert run_helper(_argv(), p) == EXIT_MAP
    assert _status(p).get("state") == STATE_STARTING  # nothing could be written


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["1", "2", "3", "4", "5"],
        ["1", "2", "3", "4", "5", "x"],
        ["-1", "2", "3", "4", "5", "1"],
    ],
)
def test_bad_arguments_are_their_own_exit_code(argv: list[str]) -> None:
    assert run_helper(argv, FakePlatform()) == EXIT_ARGS


def test_a_wrong_layout_version_on_the_command_line_is_refused() -> None:
    argv = _argv()
    argv[-1] = str(LAYOUT_VERSION + 1)
    assert run_helper(argv, FakePlatform()) == EXIT_ARGS


def test_the_argv_round_trips_and_holds_only_integers() -> None:
    argv = helper_argv(HANDLES, LAYOUT_VERSION)
    assert argv[:3] == ["-I", "-m", "steerai.session.hook_helper"]
    assert all(a.isdigit() for a in argv[3:])
    assert parse_helper_argv(argv[3:]) == (HANDLES, LAYOUT_VERSION)


def test_the_hot_key_sets_kill_and_wakes_steer() -> None:
    p = FakePlatform(waits=["input", "quit"], events=["hotkey"])
    run_helper(_argv(), p)
    assert _status(p).get("kill") == 1
    assert p.calls.count("wake") >= 2  # ready, then the kill


def test_a_probe_delivery_from_the_callback_wakes_steer_and_times_the_callback() -> None:
    p = FakePlatform(waits=["input", "quit"])
    original = p.pump

    def pump() -> Iterator[str]:
        assert p.keyboard is not None
        p.keyboard(flags=LLKHF_UP, extra=RUN_TAG, vk=PROBE, event_ms=p.tick())
        yield from original()

    p.pump = pump  # type: ignore[method-assign]  # the callback fires during the pump
    run_helper(_argv(), p)
    s = _status(p)
    assert s.get("delivered_keyboard") == 1
    assert s.get("run_time_events") == 1
    assert p.calls.count("wake") >= 2


def test_an_exception_inside_a_callback_is_counted_and_never_escapes() -> None:
    p = FakePlatform(waits=["input", "quit"])
    original = p.pump

    def pump() -> Iterator[str]:
        assert p.keyboard is not None
        p.keyboard(flags=0, extra=RUN_TAG, vk=PROBE, event_ms="not a number")
        yield from original()

    p.pump = pump  # type: ignore[method-assign]  # a malformed event reaches the callback
    assert run_helper(_argv(), p) == EXIT_OK
    assert _status(p).get("callback_errors") == 1
