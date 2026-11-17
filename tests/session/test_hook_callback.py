"""The hook thread driven through a fake user32 (TK-04/05/12/13/32/33/34/44): the procedure
returns `CallNextHookEx`'s value on every path, copies no key code or position into anything
the loop reads, the thread installs, registers, unhooks and unregisters in the stated order on
its own thread, and a start failure is a refusal. No real hook is installed, so this runs on
any CI runner; the real desktop is covered by `test_hook_monitor.py`."""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.probes.win_input import STEER_EXTRA_INFO
from steerai.session import monitor as monitor_module
from steerai.session.monitor import (
    HC_ACTION,
    HOTKEY_ID,
    LLKHF_INJECTED,
    LLKHF_UP,
    LLMHF_INJECTED,
    PROBE_PERIOD_S,
    PROBE_VK,
    WH_KEYBOARD_LL,
    WH_MOUSE_LL,
    WM_APP_UNHOOK,
    WM_HOTKEY,
    WM_LBUTTONDOWN,
    WM_MOUSEMOVE,
    WM_QUIT,
    HookMonitor,
    _KeyboardHook,
    _MouseHook,
)

NEXT_HOOK_RESULT = 0x5EED
"""What the fake `CallNextHookEx` returns; the procedure must hand it back untouched."""
VK_SENTINEL = 0xBEEF
SCAN_SENTINEL = 0xCAFE
TIME_SENTINEL = 0xDEAD
PT_SENTINEL = (31337, 27182)
HOOK_THREAD = "steer-monitor"
FIRST_HOOK, SECOND_HOOK = 0x1001, 0x1002


class _Entry:
    """A Win32 entry point stand-in: callable, and it takes `argtypes`/`restype` like a ctypes
    function (a bound method would refuse the attribute)."""

    def __init__(self, fn: Callable[..., Any]) -> None:
        self._fn = fn

    def __call__(self, *args: Any) -> Any:
        return self._fn(*args)


class FakeKernel32:
    def __init__(self) -> None:
        self.GetCurrentThreadId = _Entry(threading.get_native_id)


class FakeUser32:
    """The user32 surface the hook thread touches, recording every call with the name of the
    thread that made it; `GetMessageW` serves a queue the test and `close()` both feed."""

    def __init__(
        self,
        *,
        hooks_ok: int = 2,
        register_ok: bool = True,
        set_hook_raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], str]] = []
        self.procs: dict[int, Callable[[int, int, int], int]] = {}
        self.messages: queue.Queue[Any] = queue.Queue()
        self._hooks_ok = hooks_ok
        self._register_ok = register_ok
        self._set_hook_raises = set_hook_raises
        self._next_hook = 0x1000
        self._live: dict[int, int] = {}  # handle -> hook kind
        self.SetWindowsHookExW = _Entry(self._set_hook)
        self.CallNextHookEx = _Entry(self._call_next)
        self.UnhookWindowsHookEx = _Entry(self._unhook)
        self.RegisterHotKey = _Entry(self._register)
        self.UnregisterHotKey = _Entry(self._unregister)
        self.PeekMessageW = _Entry(self._peek)
        self.GetMessageW = _Entry(self._get_message)
        self.PostThreadMessageW = _Entry(self._post_thread_message)
        self.TranslateMessage = _Entry(lambda _p: self._record("TranslateMessage"))
        self.DispatchMessageW = _Entry(lambda _p: self._record("DispatchMessageW"))

    def _record(self, name: str, *args: Any) -> int:
        self.calls.append((name, args, threading.current_thread().name))
        return 0

    def named(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args, _ in self.calls if called == name]

    def threads_of(self, name: str) -> list[str]:
        return [thread for called, _, thread in self.calls if called == name]

    def _set_hook(self, kind: int, proc: Any, _hinst: Any, _tid: int) -> int:
        self._record("SetWindowsHookExW", kind)
        if self._set_hook_raises is not None:
            raise self._set_hook_raises
        if len(self._live) >= self._hooks_ok:
            return 0
        self.procs[kind] = proc
        self._next_hook += 1
        self._live[self._next_hook] = kind
        return self._next_hook

    def _call_next(self, hhk: Any, code: int, wparam: int, lparam: int) -> int:
        self._record("CallNextHookEx", hhk, code, wparam, lparam)
        return NEXT_HOOK_RESULT

    def _unhook(self, hook: int) -> int:
        self._record("UnhookWindowsHookEx", hook)
        kind = self._live.pop(hook, None)
        if kind is not None:
            self.procs.pop(kind, None)
        return 1

    def _register(self, _hwnd: Any, hotkey_id: int, flags: int, vk: int) -> int:
        self._record("RegisterHotKey", hotkey_id, flags, vk)
        return 1 if self._register_ok else 0

    def _unregister(self, _hwnd: Any, hotkey_id: int) -> int:
        self._record("UnregisterHotKey", hotkey_id)
        return 1

    def _peek(self, *_args: Any) -> int:
        self._record("PeekMessageW")
        return 0

    def _get_message(self, pmsg: Any, _hwnd: Any, _lo: int, _hi: int) -> int:
        while True:
            item = self.messages.get()
            if callable(item):
                item()
                continue
            message, wparam, lparam = item
            if message == "error":
                return -1
            if message == WM_QUIT:
                return 0
            msg = ctypes.cast(pmsg, ctypes.POINTER(wintypes.MSG)).contents
            msg.message = message
            msg.wParam = wparam
            msg.lParam = lparam
            return 1

    def _post_thread_message(self, _tid: int, message: int, wparam: int, lparam: int) -> int:
        self._record("PostThreadMessageW", message)
        self.messages.put((message, wparam, lparam))
        return 1

    # ---- the test's side ------------------------------------------------------------------------

    def post(self, message: int | str, wparam: int = 0, lparam: int = 0) -> None:
        self.messages.put((message, wparam, lparam))

    def settle(self) -> None:
        """Returns once the hook thread has consumed everything posted before this call."""
        done = threading.Event()
        self.messages.put(done.set)
        assert done.wait(timeout=5.0), "the hook thread stopped pumping"


def _patch(monkeypatch: pytest.MonkeyPatch, user32: FakeUser32) -> None:
    monkeypatch.setattr(monitor_module, "_user32", lambda: user32)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: FakeKernel32(), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *_a: lambda f: f, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1400, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")


def _started(monkeypatch: pytest.MonkeyPatch, user32: FakeUser32, **kwargs: Any) -> HookMonitor:
    _patch(monkeypatch, user32)
    monitor = HookMonitor(monotonic=lambda: 100.0, **kwargs)
    monitor.start()
    return monitor


def _mouse(*, flags: int, extra: int, pt: tuple[int, int] = PT_SENTINEL) -> _MouseHook:
    info = _MouseHook()
    info.pt.x, info.pt.y = pt
    info.mouseData = 0
    info.flags = flags
    info.time = TIME_SENTINEL
    info.dwExtraInfo = extra
    return info


def _key(*, flags: int, extra: int) -> _KeyboardHook:
    info = _KeyboardHook()
    info.vkCode = VK_SENTINEL
    info.scanCode = SCAN_SENTINEL
    info.flags = flags
    info.time = TIME_SENTINEL
    info.dwExtraInfo = extra
    return info


# ---- TK-04/32: the procedure always returns CallNextHookEx's value ------------------------------


def test_an_action_is_fed_to_the_core_and_call_next_hook_ex_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32)
    try:
        info = _mouse(flags=0, extra=0)
        address = ctypes.addressof(info)
        assert user32.procs[WH_MOUSE_LL](HC_ACTION, WM_MOUSEMOVE, address) == NEXT_HOOK_RESULT
        assert user32.named("CallNextHookEx") == [(None, HC_ACTION, WM_MOUSEMOVE, address)]
        assert monitor.counts() == {"user": 1}
        key = _key(flags=0, extra=STEER_EXTRA_INFO)
        assert user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x100, ctypes.addressof(key)) == (
            NEXT_HOOK_RESULT
        )
        assert monitor.counts() == {"user": 1, "steer": 1}
    finally:
        monitor.close()


@pytest.mark.parametrize("code", [-1, 1, 3])
def test_a_code_other_than_hc_action_passes_through_without_reading_the_struct(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32)
    try:
        # lparam 0: a dereference here would be a NULL read
        assert user32.procs[WH_MOUSE_LL](code, WM_MOUSEMOVE, 0) == NEXT_HOOK_RESULT
        assert user32.procs[WH_KEYBOARD_LL](code, 0x100, 0) == NEXT_HOOK_RESULT
        assert monitor.counts() == {}
        assert monitor.healthy() is True
        assert user32.named("CallNextHookEx") == [
            (None, code, WM_MOUSEMOVE, 0),
            (None, code, 0x100, 0),
        ]
    finally:
        monitor.close()


def test_an_exception_in_the_procedure_still_returns_call_next_hook_ex_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32)
    try:
        # a NULL lparam at HC_ACTION: the cast raises inside the procedure
        assert user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x100, 0) == NEXT_HOOK_RESULT
        assert monitor.counts() == {"callback_errors": 1}
        assert monitor.healthy() is False
        assert monitor.detail() == "monitor_lost"

        def boom(**_kwargs: Any) -> None:
            raise RuntimeError("core broke")

        monkeypatch.setattr(monitor._core, "on_mouse", boom)
        info = _mouse(flags=0, extra=0)
        assert user32.procs[WH_MOUSE_LL](HC_ACTION, WM_MOUSEMOVE, ctypes.addressof(info)) == (
            NEXT_HOOK_RESULT
        )
        assert monitor.counts() == {"callback_errors": 2}
        assert len(user32.named("CallNextHookEx")) == 2
    finally:
        monitor.close()


# ---- TK-05: no key code, scan code, time or position reaches what the loop reads --------------


def test_the_procedure_keeps_no_key_code_scan_code_time_or_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32)
    try:
        structs = [
            _key(flags=0, extra=0),
            _key(flags=0, extra=STEER_EXTRA_INFO),
            _mouse(flags=0, extra=0),
            _mouse(flags=LLMHF_INJECTED, extra=STEER_EXTRA_INFO),
        ]
        kinds = (WH_KEYBOARD_LL, WH_KEYBOARD_LL, WH_MOUSE_LL, WH_MOUSE_LL)
        wparams = (0x100, 0x100, WM_MOUSEMOVE, WM_LBUTTONDOWN)
        for kind, wparam, info in zip(kinds, wparams, structs, strict=True):
            user32.procs[kind](HC_ACTION, wparam, ctypes.addressof(info))
            # scanned after every event: a later event must not be able to mask an earlier leak
            # [V2.60] PR 1: the core is a classifier and a ledger; scan both, never their reprs
            core = monitor._core
            parts = (vars(core), vars(core._classifier), vars(core._ledger))
            state = {
                f"{i}{k}": v
                for i, part in enumerate(parts)
                for k, v in part.items()
                if k not in {"_reference", "_classifier", "_ledger"}
            }
            blob = repr(state) + repr(monitor.counts()) + repr(monitor.last_user_input())
            for sentinel in (VK_SENTINEL, SCAN_SENTINEL, TIME_SENTINEL, *PT_SENTINEL):
                assert str(sentinel) not in blob, f"{sentinel} leaked into {blob}"
        assert monitor.counts() == {"user": 2, "steer": 2}
        # the dead band's reference is the one place a position lives, and it never leaves
        assert monitor._core._classifier._reference == PT_SENTINEL
        assert not any(isinstance(v, (list, tuple)) for v in state.values())
        # #373 review: every scanned value is a scalar or a dict of scalars, so no composite
        # field can hide its contents behind a default repr and make the scan vacuous
        scalar = (int, float, str, bool, type(None))
        assert all(
            isinstance(v, scalar)
            or (isinstance(v, dict) and all(isinstance(x, scalar) for x in v.values()))
            for v in state.values()
        ), state
    finally:
        monitor.close()


# ---- TK-12/13: start failures are refusals; close is ordered and bounded ------------------------


def test_a_failed_hook_install_is_a_refusal_that_unhooks_what_was_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32(hooks_ok=1)  # the mouse hook installs, the keyboard hook does not
    _patch(monkeypatch, user32)
    monitor = HookMonitor()
    with pytest.raises(SteerError) as info:
        monitor.start()
    assert info.value.code is ErrorCode.GATE_BLOCKED
    assert info.value.message.startswith("monitor_unavailable")
    assert user32.named("UnhookWindowsHookEx") == [(FIRST_HOOK,)]
    assert len(user32.named("RegisterHotKey")) == 1  # the kill key goes first ([V2.80])
    assert user32.named("UnregisterHotKey") == [(HOTKEY_ID,)]  # and is given back
    assert monitor.healthy() is False
    monitor.close()
    monitor.close()


def test_a_taken_hot_key_is_a_refusal_that_unhooks_both_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32(register_ok=False)
    _patch(monkeypatch, user32)
    monitor = HookMonitor()
    with pytest.raises(SteerError) as info:
        monitor.start()
    assert info.value.code is ErrorCode.GATE_BLOCKED
    assert info.value.message.startswith("kill_key_unavailable")
    assert "--kill-key" in info.value.message
    assert user32.named("SetWindowsHookExW") == [], "[V2.80]: no hook before the kill key"
    assert user32.named("UnhookWindowsHookEx") == []
    assert user32.named("UnregisterHotKey") == [], "never registered, never unregistered"
    monitor.close()


def test_an_exception_in_the_hook_thread_before_ready_is_a_start_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32(set_hook_raises=OSError("access denied"))
    _patch(monkeypatch, user32)
    monitor = HookMonitor()
    try:
        with pytest.raises(SteerError) as info:
            monitor.start()
        assert info.value.code is ErrorCode.GATE_BLOCKED
        assert info.value.message.startswith("monitor_unavailable")
    finally:
        monitor.close()


def test_close_unhooks_and_unregisters_on_the_hook_thread_before_quitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32)
    assert monitor.healthy() is True
    monitor.close()
    assert user32.named("PostThreadMessageW") == [(WM_APP_UNHOOK,), (WM_QUIT,)]
    assert user32.named("UnhookWindowsHookEx") == [(SECOND_HOOK,), (FIRST_HOOK,)]
    assert user32.threads_of("UnhookWindowsHookEx") == [HOOK_THREAD, HOOK_THREAD]
    assert user32.threads_of("RegisterHotKey") == [HOOK_THREAD]
    assert user32.threads_of("UnregisterHotKey") == [HOOK_THREAD]
    assert user32.named("UnregisterHotKey") == [(HOTKEY_ID,)]
    assert monitor.healthy() is False, "no thread after close"
    assert monitor.thread_is_daemon() is False


# ---- TK-33: the pump's three exits ---------------------------------------------------------------


def test_a_get_message_error_ends_the_pump_unhealthy_and_tears_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32)
    user32.post("error")
    monitor.close()  # joins the thread that already left the pump
    assert monitor._core.healthy(now=100.0, thread_alive=True) is False, "the loop error is kept"
    assert user32.named("UnhookWindowsHookEx") == [(SECOND_HOOK,), (FIRST_HOOK,)]
    assert user32.named("UnregisterHotKey") == [(HOTKEY_ID,)]
    assert user32.threads_of("UnregisterHotKey") == [HOOK_THREAD]


def test_the_hot_key_message_sets_the_flag_and_a_foreign_id_or_message_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32)
    try:
        user32.post(WM_HOTKEY, HOTKEY_ID + 1)
        user32.post(0x0401)  # some other posted message: dispatched, nothing else
        user32.settle()
        assert monitor.kill_requested() is False
        # the foreign hot key and the stray message both go down the ordinary dispatch path
        assert len(user32.named("TranslateMessage")) == 2
        assert len(user32.named("DispatchMessageW")) == 2
        user32.post(WM_HOTKEY, HOTKEY_ID)
        user32.settle()
        assert monitor.kill_requested() is True
        assert monitor.healthy() is True
    finally:
        monitor.close()


# ---- TK-34/44: start, close, start; the flag is sticky until a fresh start -------------------


def test_start_close_start_registers_again_and_only_a_fresh_start_clears_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32)
    user32.post(WM_HOTKEY, HOTKEY_ID)
    user32.post(WM_HOTKEY, HOTKEY_ID)  # auto-repeat or a second press: still one sticky flag
    user32.settle()
    assert monitor.kill_requested() is True
    monitor.close()
    assert monitor.kill_requested() is True, "sticky after close: the run that saw it is ending"
    monitor.start()
    try:
        assert monitor.kill_requested() is False
        assert monitor.healthy() is True
    finally:
        monitor.close()
    assert len(user32.named("RegisterHotKey")) == 2
    assert user32.named("UnregisterHotKey") == [(HOTKEY_ID,), (HOTKEY_ID,)]
    assert user32.threads_of("UnregisterHotKey") == [HOOK_THREAD, HOOK_THREAD]
    assert len(user32.named("UnhookWindowsHookEx")) == 4


@pytest.mark.parametrize(
    ("chord", "flags", "vk"),
    [
        ("ctrl+alt+shift+s", 0x4000 | 0x2 | 0x1 | 0x4, ord("S")),
        ("ctrl+alt+k", 0x4000 | 0x2 | 0x1, ord("K")),
        ("win+shift+k", 0x4000 | 0x8 | 0x4, ord("K")),
    ],
)
def test_the_hot_key_is_registered_with_the_chord_and_no_repeat(
    monkeypatch: pytest.MonkeyPatch, chord: str, flags: int, vk: int
) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32, kill_key=chord)
    try:
        assert user32.named("RegisterHotKey") == [(HOTKEY_ID, flags, vk)]
    finally:
        monitor.close()


# ---- the self-probe when SendInput refuses ----------------------------------------------------


def test_a_self_probe_that_cannot_be_sent_does_not_raise_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = FakeUser32()
    clock = {"t": 100.0}
    _patch(monkeypatch, user32)
    monitor = HookMonitor(monotonic=lambda: clock["t"])
    monitor.start()
    try:

        def refused(**_k: object) -> None:
            raise OSError("SendInput sent 0/1: error 5")

        monkeypatch.setattr(monitor_module, "nudge_in_place", refused)
        clock["t"] += PROBE_PERIOD_S
        monitor.maintain()  # the loop's boundary calls this; an exception here is a crash
        assert monitor.healthy() is False
        assert monitor.detail() == "monitor_lost"
    finally:
        monitor.close()


def test_a_started_monitor_knows_its_runs_tag_and_names_another_steer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[V2.57] LW-02, [V2.58] (2): the core `start` builds carries the run's tag; v1's constant
    is then another Steer."""
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32, extra_info=0x1234_5678)
    try:
        own = _key(flags=0, extra=0x1234_5678)
        user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x100, ctypes.addressof(own))
        assert monitor.counts() == {"steer": 1}
        other = _key(flags=0, extra=STEER_EXTRA_INFO)
        user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x100, ctypes.addressof(other))
        assert monitor.counts() == {"steer": 1, "other_steer": 1}
        assert monitor.takeover_token() == "other_steer_input"
    finally:
        monitor.close()


# ---- [V2.57] LW-01: the v2 monitor's per-hook probe and its callback latency -------------------


def _v2_started(
    monkeypatch: pytest.MonkeyPatch, user32: FakeUser32, *, tick: int = 1000
) -> HookMonitor:
    return _started(monkeypatch, user32, per_hook=True, probe_vk=PROBE_VK, tick=lambda: tick)


def test_the_v2_probe_proves_each_hook_with_its_own_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []
    monkeypatch.setattr(monitor_module, "nudge_in_place", lambda **_k: sent.append("nudge"))
    monkeypatch.setattr(
        monitor_module, "key_up_probe", lambda vk, **_k: sent.append(("key_up", vk))
    )
    monitor = _v2_started(monkeypatch, FakeUser32())
    try:
        monitor.probe_now()
    finally:
        monitor.close()
    assert sent == ["nudge", ("key_up", PROBE_VK)]
    v1 = _started(monkeypatch, FakeUser32())
    try:
        sent.clear()
        v1.probe_now()
    finally:
        v1.close()
    assert sent == ["nudge"]  # v1's probe is unchanged


def test_a_key_probe_that_cannot_be_sent_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(_vk: int, **_k: object) -> None:
        raise OSError("SendInput sent 0/1: error 5")

    monkeypatch.setattr(monitor_module, "nudge_in_place", lambda **_k: None)
    monkeypatch.setattr(monitor_module, "key_up_probe", refused)
    monitor = _v2_started(monkeypatch, FakeUser32())
    try:
        monitor.probe_now()
        assert monitor.healthy() is False
    finally:
        monitor.close()


@pytest.mark.parametrize("vk", [None, 0x41, 0x10, 0x88])
def test_the_probe_key_is_an_unassigned_virtual_key(vk: int | None) -> None:
    """Microsoft Learn, Virtual-Key Codes: 0x0E-0F, 0x97-9F and 0xE8 are Unassigned; a letter,
    a modifier or a Reserved code is never the probe."""
    with pytest.raises(ValueError, match="probe"):
        HookMonitor(per_hook=True, probe_vk=vk)


def test_the_monitor_reports_whether_both_hooks_saw_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor_module, "nudge_in_place", lambda **_k: None)
    monkeypatch.setattr(monitor_module, "key_up_probe", lambda _vk, **_k: None)
    user32 = FakeUser32()
    clock = {"t": 100.0}
    _patch(monkeypatch, user32)
    monitor = HookMonitor(
        monotonic=lambda: clock["t"], per_hook=True, probe_vk=PROBE_VK, tick=lambda: 1000
    )
    monitor.start()
    try:
        clock["t"] += 1.0  # the probe goes out after the hooks' start
        monitor.probe_now()
        clock["t"] += 0.01
        assert monitor.probe_seen() is False
        mouse = _mouse(flags=LLMHF_INJECTED, extra=STEER_EXTRA_INFO)
        user32.procs[WH_MOUSE_LL](HC_ACTION, WM_MOUSEMOVE, ctypes.addressof(mouse))
        assert monitor.probe_seen() is False
        other = _key(flags=LLKHF_INJECTED | LLKHF_UP, extra=STEER_EXTRA_INFO)  # another key
        user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x101, ctypes.addressof(other))
        assert monitor.probe_seen() is False  # #364 P1: only the probe key's key-up proves it
        key = _key(flags=LLKHF_INJECTED | LLKHF_UP, extra=STEER_EXTRA_INFO)
        key.vkCode = PROBE_VK
        user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x101, ctypes.addressof(key))
        assert monitor.probe_seen() is True
    finally:
        monitor.close()


def test_the_v2_monitor_records_each_callbacks_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """[V2.57] LW-01: event time (the struct's tick) against entry time (the tick on entry), in
    milliseconds, kept only as a fixed histogram; a wrapped tick counter still subtracts."""
    user32 = FakeUser32()
    monitor = _v2_started(monkeypatch, user32, tick=TIME_SENTINEL + 10)
    try:
        for late in (0, 10):
            key = _key(flags=0, extra=0)
            key.time = TIME_SENTINEL + 10 - late
            user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x100, ctypes.addressof(key))
        assert monitor.latency() == (2, 10, 10)
    finally:
        monitor.close()
    user32 = FakeUser32()
    wrapped = _v2_started(monkeypatch, user32, tick=5)
    try:
        key = _key(flags=0, extra=0)
        key.time = 0xFFFF_FFFF - 4  # ten milliseconds before the tick counter wrapped
        user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x100, ctypes.addressof(key))
        assert wrapped.latency() == (1, 10, 10)
    finally:
        wrapped.close()


def test_latency_percentiles_from_the_histogram() -> None:
    from steerai.session.monitor import HookLatency  # noqa: PLC0415 - this test only

    latency = HookLatency()
    assert latency.summary() == (0, None, None)
    for ms in [1] * 95 + [20] * 4 + [5000]:
        latency.record(ms)
    assert latency.summary() == (100, 1, 20)  # rank 95 is 1 ms, rank 99 is 20 ms
    latency.record(-3)  # a tick from before the counter wrapped is never negative
    assert latency.summary()[0] == 101


def test_v1_records_no_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    user32 = FakeUser32()
    monitor = _started(monkeypatch, user32, tick=lambda: 1000)  # a tick given, v1 still times none
    try:
        key = _key(flags=0, extra=0)
        user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x100, ctypes.addressof(key))
        assert monitor.latency() == (0, None, None)
    finally:
        monitor.close()


def test_a_tick_that_raises_still_passes_the_event_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """#364 H6: the entry tick is read inside the procedure's try: the chain goes on and the
    failure is recorded, never raised into the hook chain."""

    def broken_tick() -> int:
        raise OSError("GetTickCount")

    user32 = FakeUser32()
    _patch(monkeypatch, user32)
    monitor = HookMonitor(
        monotonic=lambda: 100.0, per_hook=True, probe_vk=PROBE_VK, tick=broken_tick
    )
    monitor.start()
    try:
        key = _key(flags=0, extra=0)
        assert user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x100, ctypes.addressof(key)) == (
            NEXT_HOOK_RESULT
        )
        assert monitor.counts().get("callback_errors") == 1
    finally:
        monitor.close()


def test_the_stall_seam_blocks_only_the_named_hooks_next_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#364 P3: the seam the Windows removal test uses, proved here over the fake user32."""
    user32 = FakeUser32()
    monitor = _v2_started(monkeypatch, user32)

    def timed(kind: int, wparam: int, info: ctypes.Structure) -> float:
        started = time.perf_counter()
        user32.procs[kind](HC_ACTION, wparam, ctypes.addressof(info))
        return time.perf_counter() - started

    try:
        monitor.stall_next_for_test("keyboard", 0.2)
        assert timed(WH_MOUSE_LL, WM_MOUSEMOVE, _mouse(flags=0, extra=0)) < 0.2
        assert timed(WH_KEYBOARD_LL, 0x100, _key(flags=0, extra=0)) >= 0.2
        assert timed(WH_KEYBOARD_LL, 0x100, _key(flags=0, extra=0)) < 0.2  # once only
        with pytest.raises(ValueError, match="hook"):
            monitor.stall_next_for_test("touch", 1.0)
    finally:
        monitor.close()


def test_the_latency_histogram_answers_any_quantile_and_its_max() -> None:
    """For the probe's hook-side measurement: p50, p95 and the largest bucket seen."""
    from steerai.session.monitor import HookLatency  # noqa: PLC0415 - this test only

    latency = HookLatency()
    assert latency.at(0.5) is None
    for ms in [1] * 50 + [7] * 45 + [30] * 4 + [250]:
        latency.record(ms)
    assert (latency.at(0.5), latency.at(0.95), latency.at(1.0)) == (1, 7, 250)
    for q in (0.0, 1.5):
        with pytest.raises(ValueError, match="quantile"):
            latency.at(q)


def test_an_event_stamped_after_the_entry_tick_is_zero_not_a_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#364 tester T1 (f2_nohalf): the struct's tick one past the entry tick wraps to 2^32-1;
    it is skew, recorded as 0 ms, never as the 1000 ms overflow bucket."""
    user32 = FakeUser32()
    monitor = _v2_started(monkeypatch, user32, tick=TIME_SENTINEL)
    try:
        key = _key(flags=0, extra=0)
        key.time = TIME_SENTINEL + 1
        user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x100, ctypes.addressof(key))
        assert monitor.latency() == (1, 0, 0)
    finally:
        monitor.close()


def test_the_unhook_seam_can_remove_one_hook_the_way_windows_would(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#364 CI: Windows did not remove a hook after stalled callbacks (measured: 1 to 8 stalls
    of 2 s, every later probe delivered), so the removal test removes it for real, one hook."""
    user32 = FakeUser32()
    monitor = _v2_started(monkeypatch, user32)
    try:
        monitor.unhook_for_test("keyboard")
        assert WH_KEYBOARD_LL not in user32.procs
        assert WH_MOUSE_LL in user32.procs
        with pytest.raises(ValueError, match="hook"):
            monitor.unhook_for_test("touch")
    finally:
        monitor.close()


def test_a_stalled_probe_that_counts_late_cannot_prove_a_probe_sent_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows passes a timed-out event on, but the stalled callback still runs and counts it
    late; that late count proves its own probe only, never one sent after it."""
    monkeypatch.setattr(monitor_module, "nudge_in_place", lambda **_k: None)
    monkeypatch.setattr(monitor_module, "key_up_probe", lambda _vk, **_k: None)
    user32 = FakeUser32()
    clock = {"t": 100.0}
    _patch(monkeypatch, user32)
    monitor = HookMonitor(
        monotonic=lambda: clock["t"], per_hook=True, probe_vk=PROBE_VK, tick=lambda: 1000
    )
    monitor.start()
    try:
        clock["t"] += 1.0
        monitor.probe_now()  # A, whose callbacks stall
        clock["t"] += 0.01
        monitor.probe_now()  # B, sent while A is still out
        key = _key(flags=LLKHF_INJECTED | LLKHF_UP, extra=STEER_EXTRA_INFO)
        key.vkCode = PROBE_VK
        move = _mouse(flags=LLMHF_INJECTED, extra=STEER_EXTRA_INFO)
        user32.procs[WH_MOUSE_LL](HC_ACTION, WM_MOUSEMOVE, ctypes.addressof(move))
        user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x101, ctypes.addressof(key))  # A, late
        assert monitor.probe_seen() is False
        user32.procs[WH_MOUSE_LL](HC_ACTION, WM_MOUSEMOVE, ctypes.addressof(move))
        user32.procs[WH_KEYBOARD_LL](HC_ACTION, 0x101, ctypes.addressof(key))  # B
        assert monitor.probe_seen() is True
    finally:
        monitor.close()


def test_the_probe_carries_the_monitors_own_tag_not_the_processs_current_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[V2.80]: the loss watch probes after `restore_extra_info` put v1's tag back; a probe
    tagged by the process's current tag would not be this run's own event."""
    tags: list[tuple[str, int | None]] = []
    monkeypatch.setattr(
        monitor_module, "nudge_in_place", lambda *, tag=None: tags.append(("nudge", tag))
    )
    monkeypatch.setattr(
        monitor_module, "key_up_probe", lambda _vk, *, tag=None: tags.append(("key_up", tag))
    )
    monitor = _started(
        monkeypatch, FakeUser32(), per_hook=True, probe_vk=PROBE_VK, extra_info=0x1234_5678,
        tick=lambda: 1000,
    )  # fmt: skip
    try:
        monitor.probe_now()
    finally:
        monitor.close()
    assert tags == [("nudge", 0x1234_5678), ("key_up", 0x1234_5678)]


def test_a_second_steer_fails_on_the_kill_key_before_any_hook_or_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[V2.80]: inside a loss watch the first Steer still holds the kill key; the next one is
    refused there, with no hook installed and nothing sent."""
    sent: list[str] = []
    monkeypatch.setattr(monitor_module, "nudge_in_place", lambda **_k: sent.append("nudge"))
    monkeypatch.setattr(monitor_module, "key_up_probe", lambda _vk, **_k: sent.append("key_up"))
    user32 = FakeUser32(register_ok=False)  # the chord is held by the first Steer
    _patch(monkeypatch, user32)
    second = HookMonitor(per_hook=True, probe_vk=PROBE_VK, tick=lambda: 1000)
    with pytest.raises(SteerError) as info:
        second.start()
    assert info.value.message.startswith("kill_key_unavailable")
    assert user32.named("SetWindowsHookExW") == []
    assert sent == []
    second.close()
