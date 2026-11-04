"""Takeover monitor (design 5.1 [v1.4]): the pure core that the hook thread feeds and the loop
reads.

Rules (edge cases TK-01..49, probe `hook-latency-2026-11-04`):
- Only Steer's own `dwExtraInfo` tag exempts an event. Any other keyboard, button or wheel
  event is the person's, injected bit or not (RDP, VMs, remappers, pen and touch all inject);
  an injected bare mouse move is counted but is never a takeover.
- Real mouse moves pass a dead band around the last cursor position any move set, so a nudged
  desk is not a takeover; wheel and button events always count.
- Input while an ask is open is the person's answer, not a takeover.
- Health is proven, not assumed: our own tagged events are a free probe; after a quiet period
  a self-probe is due (the caller injects an absolute move to the current cursor position) and
  must be seen within a deadline, or the hook was removed by the OS and the monitor is lost.
- The core keeps O(1) counters and one `last_user` instant: no key codes, no positions beyond
  the dead-band reference, no event list, nothing that could carry what the person typed.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from steerai.action.keyboard import MODIFIERS, chord_signature, is_blocked_combo, is_routed_combo
from steerai.errors import ErrorCode, SteerError
from steerai.probes.win_input import (
    STEER_EXTRA_INFO,
    ULONG_PTR,
    _send,
    _user32,
    build_keyboard_input,
    nudge_in_place,
)

LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_MOUSEWHEEL = 0x020A
DEAD_BAND_PX = 4
"""Physical pixels; unmeasured across DPI settings (TK-42)."""
PROBE_PERIOD_S = 1.0
"""Quiet time after the last tagged event before a self-probe is due (unmeasured)."""
PROBE_DEADLINE_S = 0.25
"""How long a sent probe may take to reach the hook; measured p95 under load is 48 ms, max
110 ms (probe `hook-latency-2026-11-04`)."""


class MonitorCore:
    def __init__(self, *, started_at: float, dead_band_px: int = DEAD_BAND_PX) -> None:
        self._started_at = started_at
        self._dead_band = dead_band_px
        self._last_user: float | None = None
        self._last_steer: float = started_at
        self._probe_sent_at: float | None = None
        self._reference: tuple[int, int] | None = None
        self._counts: dict[str, int] = {}
        self._ask_depth = 0
        self._kill = False
        self._errors = 0
        self._loop_failed = False

    # ---- what the hook thread feeds -------------------------------------------------------------

    def on_keyboard(self, *, flags: int, extra: int, now: float) -> None:
        if extra == STEER_EXTRA_INFO:
            self._steer(now)
            return
        self._person(now, injected=bool(flags & LLKHF_INJECTED))

    def on_mouse(
        self, *, message: int, flags: int, extra: int, pt: tuple[int, int], now: float
    ) -> None:
        x, y = pt
        if extra == STEER_EXTRA_INFO:
            self._steer(now)
            if message == WM_MOUSEMOVE:
                self._reference = (x, y)
            return
        injected = bool(flags & LLMHF_INJECTED)
        if message != WM_MOUSEMOVE:
            self._person(now, injected=injected)
            return
        moved = self._reference is None or (
            abs(x - self._reference[0]) > self._dead_band
            or abs(y - self._reference[1]) > self._dead_band
        )
        if injected:
            self._reference = (x, y)
            self._count("injected_other")
            return
        if moved:
            # the reference moves only with a counted move, so a slow drift still adds up
            self._reference = (x, y)
            self._person(now, injected=False)
        else:
            self._count("user")

    def on_hotkey(self) -> None:
        self._kill = True

    def on_callback_error(self) -> None:
        self._errors += 1
        self._count("callback_errors")

    def on_loop_error(self) -> None:
        self._loop_failed = True

    def on_probe_failed(self) -> None:
        """The self-probe could not be sent (UIPI, a secure desktop): the hook is unproven."""
        self._loop_failed = True

    def mark_ask_open(self) -> None:
        self._ask_depth += 1

    def mark_ask_closed(self) -> None:
        self._ask_depth = max(0, self._ask_depth - 1)

    # ---- what the loop reads ----------------------------------------------------------------

    def last_user_input(self) -> float:
        """The last instant the person acted; the monitor's start time until the first one
        (unknown is treated as "just now", TK-29)."""
        return self._started_at if self._last_user is None else self._last_user

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def kill_requested(self) -> bool:
        return self._kill

    def probe_due(self, *, now: float) -> bool:
        if self._probe_sent_at is not None and self._probe_sent_at >= self._last_steer:
            return False  # one probe in flight
        return now - self._last_steer >= PROBE_PERIOD_S

    def probe_sent(self, *, now: float) -> None:
        self._probe_sent_at = now

    def healthy(self, *, now: float, thread_alive: bool) -> bool:
        if not thread_alive or self._errors or self._loop_failed:
            return False
        sent = self._probe_sent_at
        probe_missed = (
            sent is not None and self._last_steer < sent and now - sent > PROBE_DEADLINE_S
        )
        return not probe_missed

    def detail(self) -> str:
        """The 5.4 detail token for an unhealthy monitor."""
        return "monitor_lost"

    # ---- internals -----------------------------------------------------------------------------

    def _steer(self, now: float) -> None:
        self._last_steer = now
        self._count("steer")

    def _person(self, now: float, *, injected: bool) -> None:
        if self._ask_depth > 0:
            self._count("answer")
            return
        self._count("injected_other" if injected else "user")
        self._last_user = now

    def _count(self, kind: str) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + 1


# ---- the kill key ----------------------------------------------------------------------------

DEFAULT_KILL_KEY = "ctrl+alt+shift+s"
_VK_F12 = 0x7B
_MOD_FLAGS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
_MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0x05EE


def parse_kill_key(chord: str) -> tuple[frozenset[str], int]:
    """The kill chord as (modifiers, virtual key): at least one modifier, never F12 (the
    debugger's), never a blocked or routed combo (TK-18)."""
    try:
        mods, vk = chord_signature(chord)
    except SteerError as exc:
        raise ValueError(f"kill key {chord!r}: {exc.message}") from exc
    if not mods:
        raise ValueError(f"kill key {chord!r} needs at least one modifier")
    if vk == _VK_F12:
        raise ValueError(f"kill key {chord!r}: F12 is reserved by the debugger")
    if is_blocked_combo(chord) or is_routed_combo(chord):
        raise ValueError(f"kill key {chord!r} is a blocked or routed shortcut")
    return mods, vk


# ---- the hook thread (Windows) -------------------------------------------------------------

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_APP_UNHOOK = 0x8000 + 1
WM_APP_UNHOOK_KEEP_KEY = 0x8000 + 2
PM_NOREMOVE = 0


class _KeyboardHook(ctypes.Structure):
    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _MouseHook(ctypes.Structure):
    _fields_ = (
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HookMonitor:
    """One dedicated daemon thread owns both low-level hooks and the kill hot key, pumps
    `GetMessageW`, and feeds a `MonitorCore`. The callback reads two fields, hands them to the
    core, and always returns `CallNextHookEx`'s value (TK-04/05/21/32/33); unhook and unregister
    run on the owning thread (TK-34); `start()` waits for ready-or-error (TK-13); `close()` is
    bounded (TK-12)."""

    def __init__(
        self,
        *,
        kill_key: str = DEFAULT_KILL_KEY,
        monotonic: Callable[[], float] = time.monotonic,
        dead_band_px: int = DEAD_BAND_PX,
    ) -> None:
        self._mods, self._vk = parse_kill_key(kill_key)
        self._monotonic = monotonic
        self._dead_band = dead_band_px
        self._core = MonitorCore(started_at=monotonic(), dead_band_px=dead_band_px)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: str | None = None
        self._procs: list[Any] = []  # the WINFUNCTYPE objects must outlive the hooks
        self._stop_timed_out = False
        self._hotkey_registered = False

    # ---- lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        if sys.platform != "win32":
            raise SteerError(ErrorCode.GATE_BLOCKED, "monitor_unavailable: hooks require Windows")
        if self._stop_timed_out:
            raise SteerError(
                ErrorCode.GATE_BLOCKED,
                "monitor_unavailable: a previous hook thread did not stop; restart the process",
            )
        self._core = MonitorCore(started_at=self._monotonic(), dead_band_px=self._dead_band)
        self._ready.clear()
        self._error = None
        self._procs = []
        self._thread = threading.Thread(target=self._run, name="steer-monitor", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            self._error = "monitor_unavailable: the hook thread did not start"
        if self._error is not None:
            self.close()
            raise SteerError(ErrorCode.GATE_BLOCKED, self._error)

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._ready.wait(timeout=5.0)  # the thread id exists once the queue does
        if thread.is_alive() and self._thread_id:
            user32 = _user32()
            user32.PostThreadMessageW(self._thread_id, WM_APP_UNHOOK, 0, 0)
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=2.0)
        self._stop_timed_out = thread.is_alive()
        self._thread = None

    def thread_is_daemon(self) -> bool:
        return self._thread is not None and self._thread.daemon

    # ---- what the loop and the confirmer ask -------------------------------------------------

    def last_user_input(self) -> float:
        return self._core.last_user_input()

    def counts(self) -> dict[str, int]:
        return self._core.counts()

    def kill_requested(self) -> bool:
        return self._core.kill_requested()

    def healthy(self) -> bool:
        alive = self._thread is not None and self._thread.is_alive()
        return self._core.healthy(now=self._monotonic(), thread_alive=alive)

    def detail(self) -> str:
        return self._core.detail()

    def maintain(self) -> None:
        """Called at every loop boundary: sends the self-probe when the hook has been quiet."""
        if self._core.probe_due(now=self._monotonic()):
            self.probe_now()

    def probe_now(self) -> None:
        self._core.probe_sent(now=self._monotonic())
        try:
            nudge_in_place()
        except OSError:
            self._core.on_probe_failed()  # fails closed: the next boundary stops (D4)

    def mark_ask_open(self) -> None:
        self._core.mark_ask_open()

    def mark_ask_closed(self) -> None:
        self._core.mark_ask_closed()

    # ---- test seams (real desktop, Windows tests only) ------------------------------------------

    def press_kill_key_for_test(self) -> None:
        """Inject the registered chord with Steer's tag; the hot key fires on injected input."""
        order = [m for m in ("ctrl", "shift", "alt", "win") if m in self._mods]
        down = [build_keyboard_input(vk=MODIFIERS[m], keyup=False) for m in order]
        up = [build_keyboard_input(vk=MODIFIERS[m], keyup=True) for m in reversed(order)]
        _send(
            _user32(),
            *down,
            build_keyboard_input(vk=self._vk, keyup=False),
            build_keyboard_input(vk=self._vk, keyup=True),
            *up,
        )

    def unhook_for_test(self) -> None:
        """Remove the hooks behind the core's back, the way the OS does on a timeout."""
        if self._thread_id:
            _user32().PostThreadMessageW(self._thread_id, WM_APP_UNHOOK_KEEP_KEY, 0, 0)
            time.sleep(0.05)

    # ---- the thread ----------------------------------------------------------------------------

    def _pump(self, user32: Any, msg: Any, hooks: list[int], *, hotkey: bool) -> bool:
        """The message loop; returns whether the hot key is still registered."""
        core = self._core
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got == 0:
                return hotkey
            if got == -1:
                core.on_loop_error()
                return hotkey
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                core.on_hotkey()
            elif msg.message == WM_APP_UNHOOK:
                _unhook_all(user32, hooks)
                if hotkey:
                    user32.UnregisterHotKey(None, HOTKEY_ID)
                    hotkey = False
            elif msg.message == WM_APP_UNHOOK_KEEP_KEY:
                _unhook_all(user32, hooks)
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

    def _run(self) -> None:
        user32 = _user32()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hookproc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int,
            hookproc,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        )
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)  # creates the queue first
        self._thread_id = int(kernel32.GetCurrentThreadId())
        hooks: list[int] = []
        try:
            self._install_and_pump(user32, hookproc, msg, hooks)
        except Exception as exc:  # noqa: BLE001 - the thread reports, start() refuses (D3)
            if self._error is None:
                self._error = f"monitor_unavailable: {type(exc).__name__} in the hook thread"
            self._core.on_loop_error()
        finally:
            _unhook_all(user32, hooks)
            if self._hotkey_registered:
                user32.UnregisterHotKey(None, HOTKEY_ID)
                self._hotkey_registered = False
            self._ready.set()

    def _make_proc(self, user32: Any, hookproc: Any, *, mouse: bool) -> Any:
        """The hook procedure: two fields to the core, then always `CallNextHookEx`
        (TK-04/05/32)."""
        core = self._core
        monotonic = self._monotonic
        struct = _MouseHook if mouse else _KeyboardHook

        def proc(code: int, wparam: int, lparam: int) -> int:
            try:
                if code == HC_ACTION:
                    info = ctypes.cast(lparam, ctypes.POINTER(struct)).contents
                    if mouse:
                        core.on_mouse(
                            message=int(wparam),
                            flags=int(info.flags),
                            extra=int(info.dwExtraInfo),
                            pt=(int(info.pt.x), int(info.pt.y)),
                            now=monotonic(),
                        )
                    else:
                        core.on_keyboard(
                            flags=int(info.flags), extra=int(info.dwExtraInfo), now=monotonic()
                        )
            except Exception:  # noqa: BLE001 - the hook chain must go on; the core records it
                core.on_callback_error()
            return int(user32.CallNextHookEx(None, code, wparam, lparam))

        return hookproc(proc)

    def _install_and_pump(self, user32: Any, hookproc: Any, msg: Any, hooks: list[int]) -> None:
        for mouse, kind in ((True, WH_MOUSE_LL), (False, WH_KEYBOARD_LL)):
            proc = self._make_proc(user32, hookproc, mouse=mouse)
            self._procs.append(proc)
            hook = user32.SetWindowsHookExW(kind, proc, None, 0)
            if not hook:
                error = ctypes.get_last_error()
                self._error = f"monitor_unavailable: SetWindowsHookEx failed (error {error})"
                return
            hooks.append(int(hook))
        flags = sum(_MOD_FLAGS[m] for m in self._mods) | _MOD_NOREPEAT
        if not user32.RegisterHotKey(None, HOTKEY_ID, flags, self._vk):
            error = ctypes.get_last_error()
            self._error = (
                f"kill_key_unavailable: RegisterHotKey failed (error {error}); "
                "another program holds the chord, pass --kill-key"
            )
            return
        self._hotkey_registered = True
        self._ready.set()
        self._hotkey_registered = self._pump(user32, msg, hooks, hotkey=True)


def _unhook_all(user32: Any, hooks: list[int]) -> None:
    while hooks:
        user32.UnhookWindowsHookEx(hooks.pop())
