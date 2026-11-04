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
    LLMHF_INJECTED,
    PROBE_PERIOD_S,
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
            state = {k: v for k, v in vars(monitor._core).items() if k != "_reference"}
            blob = repr(state) + repr(monitor.counts()) + repr(monitor.last_user_input())
            for sentinel in (VK_SENTINEL, SCAN_SENTINEL, TIME_SENTINEL, *PT_SENTINEL):
                assert str(sentinel) not in blob, f"{sentinel} leaked into {blob}"
        assert monitor.counts() == {"user": 2, "steer": 2}
        # the dead band's reference is the one place a position lives, and it never leaves
        assert monitor._core._reference == PT_SENTINEL
        assert not any(isinstance(v, (list, tuple)) for v in state.values())
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
    assert user32.named("RegisterHotKey") == []
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
    assert user32.named("UnhookWindowsHookEx") == [(SECOND_HOOK,), (FIRST_HOOK,)]
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

        def refused() -> None:
            raise OSError("SendInput sent 0/1: error 5")

        monkeypatch.setattr(monitor_module, "nudge_in_place", refused)
        clock["t"] += PROBE_PERIOD_S
        monitor.maintain()  # the loop's boundary calls this; an exception here is a crash
        assert monitor.healthy() is False
        assert monitor.detail() == "monitor_lost"
    finally:
        monitor.close()
