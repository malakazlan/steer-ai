"""[V2.60] K15, the writer half (promised in #373): the helper's real hook procedures, fed real
hook structs, write no status word from the key code, the scan code, the point or the wheel data.
The procedures always return the next hook's value, error or not. No hook is installed."""

from __future__ import annotations

import ctypes
from typing import Any

from steerai.session.helper_core import HelperCore, write_config
from steerai.session.hook_classifier import LLKHF_UP, probe_tag
from steerai.session.hook_helper import (
    HC_ACTION,
    KeyboardHookStruct,
    MouseHookStruct,
    _Handlers,
    make_keyboard_proc,
    make_mouse_proc,
)
from steerai.session.hook_layout import (
    LATENCY_BUCKETS,
    RUN_TIME_BUCKETS,
    config_size,
    config_words,
    status_size,
    status_words,
)

RUN_TAG = 0x1234_5678
PROBE = 0x0E
VK = 0xA7
SCAN = 0x5C4A_77
PT = (31_337, 27_182)
WHEEL = 0x0078_0000
NEXT = 0x7EED
SENTINELS = {VK, SCAN, *PT, WHEEL}


class Platform:
    """Only what the handlers use: the clocks and the wake."""

    def __init__(self) -> None:
        self.clock = 50_000_000
        self.wakes = 0

    def tick(self) -> int:
        return self.clock // 10_000

    def qpc(self) -> int:
        self.clock += 7
        return self.clock

    def wake(self, handle: int) -> None:
        assert handle == 99
        self.wakes += 1


def _wired() -> tuple[Any, Any, Any, Platform]:  # Any: the procs and the words
    cfg = config_words(memoryview(bytearray(config_size())))
    write_config(cfg, run_tag=RUN_TAG, probe_vk=PROBE, kill_mods=7, kill_vk=0x53,
                 dead_band_px=4, heartbeat_ms=100, hook_timeout_ms=1000,
                 nonce=(1, 2))  # fmt: skip
    status = status_words(memoryview(bytearray(status_size())))
    platform = Platform()
    core = HelperCore(config=cfg, status=status, freq=10_000_000, started_qpc=1, pid=7)
    handlers = _Handlers(core, platform, wake=99)  # type: ignore[arg-type]  # a partial fake
    nexts: list[tuple[int, int, int]] = []

    def call_next(code: int, wparam: int, lparam: int) -> int:
        nexts.append((code, wparam, lparam))
        return NEXT

    kb = make_keyboard_proc(handlers.keyboard, handlers.error, call_next)
    ms = make_mouse_proc(handlers.mouse, handlers.error, call_next)
    return kb, ms, status, platform


def _everything(status: Any) -> set[int]:  # Any: the status words
    words = {status.get(n) for n in status.names()}
    buckets = {status.bucket("latency", i) for i in range(LATENCY_BUCKETS)}
    buckets |= {status.bucket("run_time", i) for i in range(RUN_TIME_BUCKETS)}
    return words | buckets


def test_no_status_word_is_written_from_the_events_content() -> None:
    kb, ms, status, platform = _wired()
    t = platform.tick()
    events = [
        (kb, 0x100, KeyboardHookStruct(vkCode=VK, scanCode=SCAN, flags=0, time=t, dwExtraInfo=0)),
        (kb, 0x101, KeyboardHookStruct(vkCode=VK, scanCode=SCAN, flags=LLKHF_UP, time=t,
                                       dwExtraInfo=RUN_TAG)),
        (ms, 0x0200, MouseHookStruct(pt=PT, mouseData=WHEEL, flags=0, time=t, dwExtraInfo=0)),
        (ms, 0x020A, MouseHookStruct(pt=PT, mouseData=WHEEL, flags=0, time=t, dwExtraInfo=0)),
    ]  # fmt: skip
    for proc, wparam, info in events:
        assert proc(HC_ACTION, wparam, ctypes.addressof(info)) == NEXT
        leaked = SENTINELS & _everything(status)
        assert not leaked, f"{leaked} reached the status words"
    assert status.get("count_user") >= 2  # the events were classified, not dropped


def test_the_probe_key_up_through_the_real_procedure_is_delivered_and_wakes_steer() -> None:
    kb, _ms, status, platform = _wired()
    info = KeyboardHookStruct(vkCode=PROBE, scanCode=0, flags=LLKHF_UP, time=platform.tick(),
                              dwExtraInfo=probe_tag(RUN_TAG, 1))  # fmt: skip
    assert kb(HC_ACTION, 0x101, ctypes.addressof(info)) == NEXT
    assert status.get("probe_seq_keyboard") == 1
    assert platform.wakes == 1


def test_a_negative_code_is_passed_on_untouched() -> None:
    kb, _ms, status, _platform = _wired()
    assert kb(-1, 0x100, 0) == NEXT
    assert status.get("latency_events") == 0


def test_a_failure_before_the_handler_is_reported_and_the_chain_goes_on() -> None:
    errors: list[str] = []

    def sink(**_event: int) -> None:
        raise RuntimeError("a decode or dispatch failure")

    for make, info in (
        (make_keyboard_proc, KeyboardHookStruct(vkCode=1, scanCode=0, flags=0, time=0)),
        (make_mouse_proc, MouseHookStruct(pt=(0, 0), mouseData=0, flags=0, time=0)),
    ):
        proc = make(sink, lambda: errors.append("error"), lambda *_args: NEXT)
        assert proc(HC_ACTION, 0x100, ctypes.addressof(info)) == NEXT
    assert errors == ["error", "error"]


def test_the_chain_goes_on_even_when_the_error_report_itself_fails() -> None:
    """#375 H8: `CallNextHookEx` runs whatever happens in the procedure."""

    def sink(**_event: int) -> None:
        raise RuntimeError("a decode or dispatch failure")

    def on_error() -> None:
        raise RuntimeError("the error report failed too")

    for make, info in (
        (make_keyboard_proc, KeyboardHookStruct(vkCode=1, scanCode=0, flags=0, time=0)),
        (make_mouse_proc, MouseHookStruct(pt=(0, 0), mouseData=0, flags=0, time=0)),
    ):
        proc = make(sink, on_error, lambda *_args: NEXT)
        assert proc(HC_ACTION, 0x100, ctypes.addressof(info)) == NEXT
