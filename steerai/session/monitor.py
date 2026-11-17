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
import math
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
    key_up_probe,
    nudge_in_place,
)
from steerai.session.hook_classifier import (
    DEAD_BAND_PX,
    HOOKS,
    LLKHF_INJECTED,
    LLKHF_UP,
    LLMHF_INJECTED,
    OTHER_STEER_INPUT,
    TAKEOVER,
    TAKEOVER_TOKENS,
    UNASSIGNED_VKS,
    WM_LBUTTONDOWN,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    Classifier,
    ProbeTags,
)
from steerai.session.probe_ledger import PROBE_DEADLINE_S, PROBE_PERIOD_S, ProbeLedger

__all__ = [
    "DEAD_BAND_PX",
    "DEFAULT_KILL_KEY",
    "HC_ACTION",
    "HOOKS",
    "HOOK_TIMEOUT_CAP_MS",
    "HOTKEY_ID",
    "LATENCY_BUCKETS_MS",
    "LLKHF_INJECTED",
    "LLKHF_UP",
    "LLMHF_INJECTED",
    "MEASURER_MARGIN_S",
    "OTHER_STEER_INPUT",
    "PM_NOREMOVE",
    "PROBE_DEADLINE_S",
    "PROBE_PERIOD_S",
    "PROBE_VK",
    "TAKEOVER",
    "TAKEOVER_TOKENS",
    "UNASSIGNED_VKS",
    "WH_KEYBOARD_LL",
    "WH_MOUSE_LL",
    "WM_APP_UNHOOK",
    "WM_APP_UNHOOK_KEEP_KEY",
    "WM_HOTKEY",
    "WM_LBUTTONDOWN",
    "WM_MOUSEMOVE",
    "WM_MOUSEWHEEL",
    "WM_QUIT",
    "HookLatency",
    "HookMonitor",
    "MonitorCore",
    "parse_hook_timeout",
    "parse_kill_key",
    "probe_deadline_s",
    "read_hook_timeout_ms",
]

HOOK_TIMEOUT_CAP_MS = 1000
"""Microsoft Learn, LowLevelKeyboardProc: on Windows 10 1709 and later the system uses 1000 ms
for any LowLevelHooksTimeout larger than that."""
MEASURER_MARGIN_S = 0.05
"""[V2.59]: what the measurer adds on top of the hook timeout: the proof's poll interval
(0.01 s) and two ticks of the 15.625 ms monotonic clock it reads, rounded up."""


def parse_hook_timeout(raw: object) -> int | None:
    """LowLevelHooksTimeout as Windows stores it (REG_SZ; a DWORD accepted), in ms, capped at
    `HOOK_TIMEOUT_CAP_MS`; None when absent, zero or not a number (#364 H7: ASCII digits)."""
    if isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
        value = int(raw.strip())
    elif isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    else:
        return None
    return min(value, HOOK_TIMEOUT_CAP_MS) if value > 0 else None


def probe_deadline_s(timeout_ms: int | None) -> float:
    """[V2.59]: the probe's deadline, derived: the hook timeout read at start (unset is the
    cap) plus `MEASURER_MARGIN_S`. An echo slower than that means Windows has likely dropped
    the hook, so the monitor is lost."""
    ms = HOOK_TIMEOUT_CAP_MS if timeout_ms is None else min(timeout_ms, HOOK_TIMEOUT_CAP_MS)
    return ms / 1000 + MEASURER_MARGIN_S


def read_hook_timeout_ms() -> int | None:  # pragma: no cover - live, Windows only
    """HKCU\\Control Panel\\Desktop LowLevelHooksTimeout, parsed and capped."""
    return parse_hook_timeout(_read_hooks_timeout())


def _read_hooks_timeout() -> object:  # pragma: no cover - live, Windows only
    """The registry value as stored; None when absent."""
    import winreg  # noqa: PLC0415 - Windows only

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
            value, _kind = winreg.QueryValueEx(key, "LowLevelHooksTimeout")
    except OSError:
        return None
    return value


PROBE_VK = 0x0E
"""[V2.57] LW-01: the keyboard hook's probe key, a key-up of an unassigned code, picked by probe
(`steerai.probes.probe_key`, 2026-11-15, this machine): 20 tagged key-ups of each of the 12
unassigned codes into our own empty Notepad changed nothing (text, caret, IME open status,
layout, foreground; none left down; the hook saw all 20); the first that passes is 0x0E. The
IME was closed and the layout en-US there: an open IME is unprobed."""
LATENCY_BUCKETS_MS = 1000
"""The latency histogram's last bucket: 1000 ms and over (the hook timeout's cap, Microsoft
Learn LowLevelKeyboardProc: Windows 10 1709 and later allow at most 1000 ms)."""


class HookLatency:
    """[V2.57] LW-01: each callback's latency (event tick to entry tick, in ms) as a fixed
    histogram: no event list, no timestamps, nothing that grows with the input."""

    def __init__(self) -> None:
        self._buckets = [0] * (LATENCY_BUCKETS_MS + 1)
        self._events = 0

    def record(self, ms: int) -> None:
        self._buckets[min(max(ms, 0), LATENCY_BUCKETS_MS)] += 1
        self._events += 1

    def summary(self) -> tuple[int, int | None, int | None]:
        """(events, p95 ms, p99 ms); None when nothing was recorded."""
        if not self._events:
            return 0, None, None
        return self._events, self._quantile(0.95), self._quantile(0.99)

    def at(self, q: float) -> int | None:
        """The latency at quantile `q` (0 < q <= 1; 1 is the largest bucket seen); None when
        nothing was recorded."""
        if not 0 < q <= 1:
            raise ValueError("a quantile is above 0 and at most 1")
        return self._quantile(q) if self._events else None

    def _quantile(self, q: float) -> int:
        rank = math.ceil(q * self._events)
        seen = 0
        for ms, count in enumerate(self._buckets):
            seen += count
            if seen >= rank:
                return ms
        return LATENCY_BUCKETS_MS  # pragma: no cover - the counts always reach `rank`


class MonitorCore:
    """The in-process monitor's core: the `Classifier` the hook thread feeds and the
    `ProbeLedger` the loop asks ([V2.60] PR 1: split so the hook helper can load the classifier
    without input-sending code; the behaviour is unchanged)."""

    def __init__(  # noqa: PLR0913 - keyword-only knobs; v1 passes none of the v2 ones
        self,
        *,
        started_at: float,
        dead_band_px: int = DEAD_BAND_PX,
        extra_info: int = STEER_EXTRA_INFO,
        per_hook: bool = False,
        probe_vk: int = PROBE_VK,
        deadline_s: float = PROBE_DEADLINE_S,
    ) -> None:
        self._started_at = started_at
        self._per_hook = per_hook
        self._classifier = Classifier(
            started_at=started_at,
            extra_info=extra_info,
            probe_vk=probe_vk,
            dead_band_px=dead_band_px,
            probe_tags=per_hook,
        )
        self._tags = ProbeTags(extra_info)
        self._ledger = ProbeLedger(per_hook=per_hook, deadline_s=deadline_s)
        """[V2.57] LW-01 (v2): each hook proves itself; v1 keeps one proof for both. [V2.59]
        (v2): the derived probe deadline; v1 keeps `PROBE_DEADLINE_S`."""
        self._probe_failed = False

    # ---- what the hook thread feeds -------------------------------------------------------------

    def on_keyboard(self, *, flags: int, extra: int, now: float, vk: int = 0) -> None:
        self._classifier.on_keyboard(flags=flags, extra=extra, now=now, vk=vk)

    def on_mouse(
        self, *, message: int, flags: int, extra: int, pt: tuple[int, int], now: float
    ) -> None:
        self._classifier.on_mouse(message=message, flags=flags, extra=extra, pt=pt, now=now)

    def on_hotkey(self) -> None:
        self._classifier.on_hotkey()

    def on_callback_error(self) -> None:
        self._classifier.on_callback_error()

    def on_loop_error(self) -> None:
        self._classifier.on_loop_error()

    def on_probe_failed(self) -> None:
        """The self-probe could not be sent (UIPI, a secure desktop): the hook is unproven."""
        self._probe_failed = True

    def mark_ask_open(self) -> None:
        self._classifier.mark_ask_open()

    def mark_ask_closed(self) -> None:
        self._classifier.mark_ask_closed()

    # ---- what the loop reads ----------------------------------------------------------------

    def last_user_input(self) -> float:
        """The last instant the person acted; the monitor's start time until the first one
        (unknown is treated as "just now", TK-29)."""
        last = self._classifier.last_user()
        return self._started_at if last is None else last

    def counts(self) -> dict[str, int]:
        return self._classifier.counts()

    def takeover_token(self) -> str:
        """Who made the latest foreign input: `takeover` (a person) or `other_steer_input`
        ([V2.58] (2))."""
        return self._classifier.last_kind()

    def kill_requested(self) -> bool:
        return self._classifier.kill_requested()

    def probe_due(self, *, now: float) -> bool:
        return self._ledger.probe_due(now=now, counts=self._classifier)

    def probe_seen(self) -> bool:
        """LW-01, #364 P1, [V2.81] #492: the last probe was seen on both hooks by its own tag,
        within its deadline (never by timestamp, never by another own event). v1 never proves
        by a probe tag: it draws none."""
        return self._ledger.probe_seen(counts=self._classifier)

    def lost_hooks(self, *, now: float) -> tuple[str, ...]:
        """LW-01: the hooks that have not seen the last probe past its deadline, in order."""
        return self._ledger.lost_hooks(now=now, counts=self._classifier)

    def probe_sent(self, *, now: float) -> int:
        """[V2.81] #492: the tag this probe's events must carry (0 in v1, which sends the run's
        tag and proves nothing by a probe tag)."""
        return self._ledger.probe_sent(now=now, begin=self._begin_probe)

    def _begin_probe(self) -> int:
        """A fresh tag, registered with the classifier before the probe is injected."""
        if not self._per_hook:
            return 0
        tag = self._tags.draw()
        self._classifier.set_probe_tag(tag)
        return tag

    def healthy(self, *, now: float, thread_alive: bool) -> bool:
        c = self._classifier
        if not thread_alive or c.errors() or c.loop_failed() or self._probe_failed:
            return False
        return self._ledger.probe_ok(now=now, counts=c)

    def detail(self) -> str:
        """The 5.4 detail token for an unhealthy monitor."""
        return "monitor_lost"


# ---- the kill key ----------------------------------------------------------------------------

DEFAULT_KILL_KEY = "ctrl+alt+shift+s"
_VK_F12 = 0x7B
MOD_FLAGS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
"""RegisterHotKey's fsModifiers (Microsoft Learn): MOD_ALT, MOD_CONTROL, MOD_SHIFT,
MOD_WIN. The one copy: the helper's kill chord uses it too (#385)."""
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

    def __init__(  # noqa: PLR0913 - keyword-only knobs; v1 passes none of the v2 ones
        self,
        *,
        kill_key: str = DEFAULT_KILL_KEY,
        monotonic: Callable[[], float] = time.monotonic,
        dead_band_px: int = DEAD_BAND_PX,
        extra_info: int = STEER_EXTRA_INFO,
        per_hook: bool = False,
        probe_vk: int | None = None,
        tick: Callable[[], int] | None = None,
        probe_deadline_s: float = PROBE_DEADLINE_S,
    ) -> None:
        if per_hook and probe_vk not in UNASSIGNED_VKS:
            raise ValueError("the keyboard probe must be an unassigned virtual key")
        self._per_hook = per_hook
        """[V2.57] LW-01 (v2): each hook proves itself, and each callback's latency is kept."""
        self._probe_vk = probe_vk
        self._deadline_s = probe_deadline_s
        self._tick = tick
        """The system tick in ms (GetTickCount, the clock of the hook structs' `time`)."""
        self._latency = HookLatency()
        self._stall: dict[str, float] = {}
        """#364 P3: a test seam, never set outside Windows tests."""
        self._mods, self._vk = parse_kill_key(kill_key)
        self._monotonic = monotonic
        self._dead_band = dead_band_px
        self._extra_info = extra_info
        self._core = MonitorCore(
            started_at=monotonic(),
            dead_band_px=dead_band_px,
            extra_info=extra_info,
            per_hook=per_hook,
            probe_vk=PROBE_VK if probe_vk is None else probe_vk,
            deadline_s=probe_deadline_s,
        )
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
        self._core = MonitorCore(
            started_at=self._monotonic(),
            dead_band_px=self._dead_band,
            extra_info=self._extra_info,
            per_hook=self._per_hook,
            probe_vk=PROBE_VK if self._probe_vk is None else self._probe_vk,
            deadline_s=self._deadline_s,
        )
        self._latency = HookLatency()
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

    @property
    def stop_timed_out(self) -> bool:
        """The last `close` returned with the hook thread still alive ([V2.80])."""
        return self._stop_timed_out

    def thread_is_daemon(self) -> bool:
        return self._thread is not None and self._thread.daemon

    # ---- what the loop and the confirmer ask -------------------------------------------------

    def last_user_input(self) -> float:
        return self._core.last_user_input()

    def takeover_token(self) -> str:
        return self._core.takeover_token()

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
        try:
            tag = self._core.probe_sent(now=self._monotonic())
            # [V2.81]: v2 sends this probe's own tag on both hooks; v1 the monitor's own tag,
            # never the process's current one (a probe after `restore_extra_info`)
            if self._per_hook:
                nudge_in_place(tag=tag)
                if self._probe_vk is not None:
                    key_up_probe(self._probe_vk, tag=tag)  # LW-01: the keyboard hook's own proof
            else:
                nudge_in_place(tag=self._extra_info)
        except (OSError, RuntimeError):  # a failed send, or no tag could be drawn
            self._core.on_probe_failed()  # fails closed: the next boundary stops (D4)

    def probe_seen(self) -> bool:
        """LW-01: both hooks saw the last probe's own tag, within its deadline."""
        return self._core.probe_seen()

    def latency(self) -> tuple[int, int | None, int | None]:
        """LW-01: (callbacks timed, p95 ms, p99 ms); v1 times none."""
        return self._latency.summary()

    def latency_at(self, q: float) -> int | None:
        """The callback latency at quantile `q`, in ms (the probe's hook-side measurement)."""
        return self._latency.at(q)

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

    def stall_next_for_test(self, hook: str, seconds: float) -> None:
        """#364 P3: the next callback of `hook` (`keyboard` or `mouse`) blocks for `seconds`
        before it returns: past LowLevelHooksTimeout the OS removes that hook silently, the
        failure LW-01 must catch (Windows tests only)."""
        if hook not in HOOKS:
            raise ValueError("hook must be keyboard or mouse")
        self._stall[hook] = seconds

    def lost_hooks(self) -> tuple[str, ...]:
        """LW-01: the hooks that missed the last probe past its deadline."""
        return self._core.lost_hooks(now=self._monotonic())

    def unhook_for_test(self, hook: str | None = None) -> None:
        """Remove the hooks behind the core's back, the way the OS removes a hook: both, or
        only `hook` (`keyboard` or `mouse`; #364: measured, a stalled callback alone does not
        make Windows remove it, so the removal test removes it for real)."""
        if hook is not None and hook not in HOOKS:
            raise ValueError("hook must be keyboard or mouse")
        if self._thread_id:
            which = 0 if hook is None else 1 + HOOKS.index(hook)
            _user32().PostThreadMessageW(self._thread_id, WM_APP_UNHOOK_KEEP_KEY, which, 0)
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
                _unhook_some(user32, hooks, int(msg.wParam))
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
        if self._per_hook and self._tick is None:
            kernel32.GetTickCount.restype = wintypes.DWORD
            self._tick = kernel32.GetTickCount
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
        tick = self._tick if self._per_hook else None
        latency = self._latency

        stall = self._stall
        hook = "mouse" if mouse else "keyboard"

        def proc(code: int, wparam: int, lparam: int) -> int:
            try:
                entered = tick() if tick is not None else 0  # #364 H6: inside the try
                if hook in stall:
                    time.sleep(stall.pop(hook))  # the test seam: a callback that hangs
                if code == HC_ACTION:
                    info = ctypes.cast(lparam, ctypes.POINTER(struct)).contents
                    if tick is not None:  # LW-01: event tick to entry tick, wrap-safe
                        late = (entered - int(info.time)) & 0xFFFF_FFFF
                        latency.record(late if late < 0x8000_0000 else 0)
                    if mouse:
                        core.on_mouse(
                            message=int(wparam),
                            flags=int(info.flags),
                            extra=int(info.dwExtraInfo),
                            pt=(int(info.pt.x), int(info.pt.y)),
                            now=monotonic(),
                        )
                    else:
                        core.on_keyboard(  # the key code is compared there, never kept
                            flags=int(info.flags),
                            extra=int(info.dwExtraInfo),
                            now=monotonic(),
                            vk=int(info.vkCode),
                        )
            except Exception:  # noqa: BLE001 - the hook chain must go on; the core records it
                core.on_callback_error()
            return int(user32.CallNextHookEx(None, code, wparam, lparam))

        return hookproc(proc)

    def _install_and_pump(self, user32: Any, hookproc: Any, msg: Any, hooks: list[int]) -> None:
        flags = sum(MOD_FLAGS[m] for m in self._mods) | _MOD_NOREPEAT
        if not user32.RegisterHotKey(None, HOTKEY_ID, flags, self._vk):  # [V2.80]: the kill key
            error = ctypes.get_last_error()  # first, so a second Steer fails here, hooks unset
            self._error = (
                f"kill_key_unavailable: RegisterHotKey failed (error {error}); "
                "another program holds the chord, pass --kill-key"
            )
            return
        self._hotkey_registered = True
        for mouse, kind in ((True, WH_MOUSE_LL), (False, WH_KEYBOARD_LL)):
            proc = self._make_proc(user32, hookproc, mouse=mouse)
            self._procs.append(proc)
            hook = user32.SetWindowsHookExW(kind, proc, None, 0)
            if not hook:
                error = ctypes.get_last_error()
                self._error = f"monitor_unavailable: SetWindowsHookEx failed (error {error})"
                return
            hooks.append(int(hook))
        self._ready.set()
        self._hotkey_registered = self._pump(user32, msg, hooks, hotkey=True)


def _unhook_all(user32: Any, hooks: list[int]) -> None:
    while hooks:
        hook = hooks.pop()
        if hook:
            user32.UnhookWindowsHookEx(hook)


def _unhook_some(user32: Any, hooks: list[int], which: int) -> None:
    """The test seam's removal: 0 every hook; 1 the keyboard's, 2 the mouse's (hooks are
    installed mouse first, keyboard second); a removed one stays as 0 in its place."""
    if which == 0:
        _unhook_all(user32, hooks)
        return
    index = {1: 1, 2: 0}.get(which)
    if index is not None and index < len(hooks) and hooks[index]:
        user32.UnhookWindowsHookEx(hooks[index])
        hooks[index] = 0
