"""The hook helper process ([V2.60], packet v2-hook-helper rev 2b §2.1): both low-level hooks and
the kill hot key, on this process's one thread, publishing words for Steer.

Run as `python -I -m steerai.session.hook_helper <config> <status> <quit> <wake> <parent> <ver>`
by Steer only (`helper_launch`). The helper never sends input and never imports input-sending
code (S6, tested). It has no stderr (its standard handles are not inherited, an accepted
deviation from rev 2b §2.1): every failure, before or after ready, is published as
`state = failed` with an error code (`run_helper`), and only a failure to map the status section
itself is an exit code alone.

Steps: map the status words, map and check the config (F15), build the core, warm it (HH-04), ask
for a working-set floor (not fatal), settle the GC, install the mouse then the keyboard hook,
register the kill hot key, publish ready and wake Steer, then loop: wait for the parent, quit,
input or the heartbeat period; pump (hook callbacks and `WM_HOTKEY` run here); account the
loop. On every path the hooks and the hot key are released on this thread (TK-34).
"""

from __future__ import annotations

import ctypes
import gc
import sys
from collections.abc import Callable, Iterator
from ctypes import wintypes
from typing import Any, Final, Protocol

from steerai.session.helper_core import (
    EXIT_ARGS,
    EXIT_FAILED,
    EXIT_MAP,
    EXIT_OK,
    HelperCore,
    config_error,
    parse_helper_argv,
    warm_up,
)
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
    config_size,
    config_words,
    status_size,
    status_words,
)

HC_ACTION: Final = 0
HOTKEY_ID: Final = 0x05EE
"""The kill hot key's id on the helper's thread (the in-process monitor uses the same value)."""
WS_FLOOR_MARGIN: Final = 4 * 1024 * 1024
"""HH-04: bytes over the warmed working set kept resident (provisional; P-trim sets it)."""

KeyboardSink = Callable[..., None]
"""`(*, flags, extra, vk, event_ms)`: one keyboard event's fields, nothing else."""
MouseSink = Callable[..., None]
"""`(*, message, flags, extra, pt, event_ms)`: one mouse event's fields, nothing else."""


class HelperStepError(Exception):
    """A platform step failed; `run_helper` publishes the code of the step it was in."""


class HelperPlatform(Protocol):
    """Everything Win32 the helper does, so `run_helper` runs over a fake in tests."""

    def map_status(self, handle: int) -> memoryview: ...
    def map_config(self, handle: int) -> memoryview: ...
    def qpc(self) -> int: ...
    def freq(self) -> int: ...
    def tick(self) -> int: ...
    def unbiased(self) -> int: ...
    def pid(self) -> int: ...
    def set_ws_floor(self) -> bool: ...
    def settle_gc(self) -> None: ...
    def install_hooks(
        self, keyboard: KeyboardSink, mouse: MouseSink, on_error: Callable[[], None]
    ) -> None: ...
    def register_hotkey(self, mods: int, vk: int) -> None: ...
    def wait(self, timeout_ms: int, parent: int, quit_event: int) -> str: ...
    def pump(self) -> Iterator[str]: ...
    def wake(self, handle: int) -> None: ...
    def release(self) -> None: ...


class _Handlers:
    """The hook callbacks' bodies: time the callback, feed the core, wake Steer on a probe
    delivery. Nothing escapes: an exception is counted (`callback_errors`)."""

    def __init__(self, core: HelperCore, platform: HelperPlatform, wake: int) -> None:
        self._core = core
        self._platform = platform
        self._wake = wake

    def keyboard(self, *, flags: int, extra: int, vk: int, event_ms: int) -> None:
        p = self._platform
        entry_ms, entry = p.tick(), p.qpc()
        try:
            if self._core.keyboard(
                flags=flags, extra=extra, vk=vk, event_ms=event_ms, entry_ms=entry_ms,
                entry_qpc=entry,
            ):  # fmt: skip
                p.wake(self._wake)
        except Exception:  # noqa: BLE001 - the hook chain must go on; the count is the report
            self._core.callback_error()
        finally:
            self._finished(entry)

    def mouse(
        self, *, message: int, flags: int, extra: int, pt: tuple[int, int], event_ms: int
    ) -> None:
        p = self._platform
        entry_ms, entry = p.tick(), p.qpc()
        try:
            if self._core.mouse(
                message=message, flags=flags, extra=extra, pt=pt, event_ms=event_ms,
                entry_ms=entry_ms, entry_qpc=entry,
            ):  # fmt: skip
                p.wake(self._wake)
        except Exception:  # noqa: BLE001 - the hook chain must go on; the count is the report
            self._core.callback_error()
        finally:
            self._finished(entry)

    def error(self) -> None:
        """A callback failed before it reached a handler (decoding the event)."""
        self._core.callback_error()

    def _finished(self, entry: int) -> None:
        try:
            self._core.finished(entry_qpc=entry, exit_qpc=self._platform.qpc())
        except Exception:  # noqa: BLE001 - the hook chain must go on; the count is the report
            self._core.callback_error()


def run_helper(args: list[str], platform: HelperPlatform) -> int:  # noqa: PLR0915 - the steps
    """The helper's whole life; returns its exit code. Every failure after the status section
    is mapped is published as `state = failed` with the code of the step it happened in."""
    try:
        handles, version = parse_helper_argv(args)
    except ValueError:
        return EXIT_ARGS
    if version != LAYOUT_VERSION:
        return EXIT_ARGS
    try:
        status = status_words(platform.map_status(handles.status))
    except (OSError, ValueError, HelperStepError):
        return EXIT_MAP
    step = ERROR_INTERNAL
    failed = False
    try:
        step = ERROR_MAP
        view = platform.map_config(handles.config)
        step = ERROR_LAYOUT
        config = config_words(view)
        if config_error(config):
            raise HelperStepError("the config section is not usable")
        step = ERROR_INTERNAL
        freq = platform.freq()
        core = HelperCore(
            config=config, status=status, freq=freq, started_qpc=platform.qpc(), pid=platform.pid()
        )
        warm_up(config, freq=freq)
        core.ws_floor(set_ok=platform.set_ws_floor())
        handlers = _Handlers(core, platform, handles.wake)
        platform.settle_gc()
        step = ERROR_HOOKS
        platform.install_hooks(handlers.keyboard, handlers.mouse, handlers.error)
        step = ERROR_HOTKEY
        platform.register_hotkey(config.get("kill_mods"), config.get("kill_vk"))
        step = ERROR_INTERNAL
        core.ready(qpc=platform.qpc(), unbiased_100ns=platform.unbiased())
        platform.wake(handles.wake)
        heartbeat_ms = config.get("heartbeat_ms")
        while True:
            step = ERROR_LOOP
            woke = platform.wait(heartbeat_ms, handles.parent, handles.quit)
            if woke in {"parent", "quit"}:
                break
            for event in platform.pump():
                if event == "hotkey":
                    core.hotkey()
                    platform.wake(handles.wake)
            step = ERROR_INTERNAL
            core.loop(qpc=platform.qpc(), unbiased_100ns=platform.unbiased())
    except Exception:  # noqa: BLE001 - no stderr: the code in the status words is the report
        failed = True
        status.set("error", step)
        status.set("state", STATE_FAILED)
    finally:
        platform.release()
    if failed:
        return EXIT_FAILED
    status.set("state", STATE_CLOSED)
    return EXIT_OK


# ---- the Windows platform ----------------------------------------------------------------------

FILE_MAP_WRITE: Final = 0x0002
FILE_MAP_READ: Final = 0x0004
WH_KEYBOARD_LL: Final = 13
WH_MOUSE_LL: Final = 14
WM_HOTKEY: Final = 0x0312
MOD_NOREPEAT: Final = 0x4000
QS_ALLINPUT: Final = 0x04FF
MWMO_INPUTAVAILABLE: Final = 0x0004
PM_REMOVE: Final = 0x0001
WAIT_TIMEOUT: Final = 0x0102
WAIT_FAILED: Final = 0xFFFF_FFFF
QUOTA_LIMITS_HARDWS_MIN_ENABLE: Final = 0x0001
QUOTA_LIMITS_HARDWS_MAX_DISABLE: Final = 0x0008
_WAKES: Final = {0: "parent", 1: "quit", 2: "input", WAIT_TIMEOUT: "timeout"}

ULONG_PTR = ctypes.c_size_t


class KeyboardHookStruct(ctypes.Structure):
    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MouseHookStruct(ctypes.Structure):
    _fields_ = (
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


CallNext = Callable[[int, int, int], int]


def make_keyboard_proc(
    sink: KeyboardSink, on_error: Callable[[], None], call_next: CallNext
) -> Callable[[int, int, int], int]:
    """The keyboard hook procedure: four fields to the sink (the scan code is never read), then
    always the next hook's value (TK-04/05/32)."""

    def proc(code: int, wparam: int, lparam: int) -> int:
        try:
            if code == HC_ACTION:
                info = ctypes.cast(lparam, ctypes.POINTER(KeyboardHookStruct)).contents
                sink(
                    flags=int(info.flags),
                    extra=int(info.dwExtraInfo),
                    vk=int(info.vkCode),
                    event_ms=int(info.time),
                )
        except Exception:  # noqa: BLE001 - the hook chain must go on; the count is the report
            on_error()
        return call_next(code, wparam, lparam)

    return proc


def make_mouse_proc(
    sink: MouseSink, on_error: Callable[[], None], call_next: CallNext
) -> Callable[[int, int, int], int]:
    """The mouse hook procedure: the message, flags, tag, point and time to the sink (wheel
    data is never read), then always the next hook's value."""

    def proc(code: int, wparam: int, lparam: int) -> int:
        try:
            if code == HC_ACTION:
                info = ctypes.cast(lparam, ctypes.POINTER(MouseHookStruct)).contents
                sink(
                    message=int(wparam),
                    flags=int(info.flags),
                    extra=int(info.dwExtraInfo),
                    pt=(int(info.pt.x), int(info.pt.y)),
                    event_ms=int(info.time),
                )
        except Exception:  # noqa: BLE001 - the hook chain must go on; the count is the report
            on_error()
        return call_next(code, wparam, lparam)

    return proc


class WindowsHelperPlatform:  # pragma: no cover - live, Windows only (tests/session windows)
    """The real Win32 side of the helper, on its one thread."""

    def __init__(self) -> None:
        self._k32: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # Any: a ctypes DLL
        self._u32: Any = ctypes.WinDLL("user32", use_last_error=True)  # Any: a ctypes DLL
        self._views: list[Any] = []  # Any: ctypes arrays over mapped views; kept alive
        self._hooks: list[int] = []
        self._procs: list[Any] = []  # Any: WINFUNCTYPE objects must outlive the hooks
        self._hotkey = False
        self._declare()

    def _declare(self) -> None:
        """#375 P1: argtypes and restype for every call (Microsoft Learn signatures; HANDLE and
        the hook handle are pointer-width), so no handle is ever converted as a C int."""
        k, u = self._k32, self._u32
        handle, dword, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
        k.GetCurrentProcess.argtypes = ()
        k.GetCurrentProcess.restype = handle
        k.GetCurrentProcessId.argtypes = ()
        k.GetCurrentProcessId.restype = dword
        k.MapViewOfFile.argtypes = (handle, dword, dword, dword, ctypes.c_size_t)
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.QueryPerformanceCounter.argtypes = (ctypes.POINTER(ctypes.c_int64),)
        k.QueryPerformanceCounter.restype = boolean
        k.QueryPerformanceFrequency.argtypes = (ctypes.POINTER(ctypes.c_int64),)
        k.QueryPerformanceFrequency.restype = boolean
        k.QueryUnbiasedInterruptTime.argtypes = (ctypes.POINTER(ctypes.c_uint64),)
        k.QueryUnbiasedInterruptTime.restype = boolean
        k.GetTickCount.argtypes = ()
        k.GetTickCount.restype = dword
        k.SetProcessWorkingSetSizeEx.argtypes = (handle, ctypes.c_size_t, ctypes.c_size_t, dword)
        k.SetProcessWorkingSetSizeEx.restype = boolean
        k.SetEvent.argtypes = (handle,)
        k.SetEvent.restype = boolean
        k.TerminateProcess.argtypes = (handle, wintypes.UINT)
        k.TerminateProcess.restype = boolean
        u.SetWindowsHookExW.restype = ctypes.c_void_p  # argtypes need the HOOKPROC type: below
        u.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM,
                                     wintypes.LPARAM)  # fmt: skip
        u.CallNextHookEx.restype = ctypes.c_ssize_t
        u.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        u.UnhookWindowsHookEx.restype = boolean
        u.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        u.RegisterHotKey.restype = boolean
        u.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        u.UnregisterHotKey.restype = boolean
        u.MsgWaitForMultipleObjectsEx.argtypes = (dword, ctypes.POINTER(handle), dword, dword,
                                                  dword)  # fmt: skip
        u.MsgWaitForMultipleObjectsEx.restype = dword
        u.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                   wintypes.UINT, wintypes.UINT)  # fmt: skip
        u.PeekMessageW.restype = boolean
        u.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        u.TranslateMessage.restype = boolean
        u.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        u.DispatchMessageW.restype = ctypes.c_ssize_t

    def _map(self, handle: int, size: int, access: int) -> memoryview:
        address = self._k32.MapViewOfFile(handle, access, 0, 0, size)
        if not address:
            raise OSError(ctypes.get_last_error(), "MapViewOfFile failed")
        array = (ctypes.c_char * size).from_address(address)
        self._views.append(array)
        return memoryview(array).cast("B")

    def map_status(self, handle: int) -> memoryview:
        return self._map(handle, status_size(), FILE_MAP_READ | FILE_MAP_WRITE)

    def map_config(self, handle: int) -> memoryview:
        return self._map(handle, config_size(), FILE_MAP_READ).toreadonly()

    def qpc(self) -> int:
        value = ctypes.c_int64()
        self._k32.QueryPerformanceCounter(ctypes.byref(value))
        return int(value.value)

    def freq(self) -> int:
        value = ctypes.c_int64()
        self._k32.QueryPerformanceFrequency(ctypes.byref(value))
        return int(value.value)

    def tick(self) -> int:
        return int(self._k32.GetTickCount())

    def unbiased(self) -> int:
        value = ctypes.c_uint64()
        self._k32.QueryUnbiasedInterruptTime(ctypes.byref(value))
        return int(value.value)

    def pid(self) -> int:
        return int(self._k32.GetCurrentProcessId())

    def set_ws_floor(self) -> bool:
        """HH-04: a hard minimum working set over what the warm-up paged in (not fatal)."""

        class Counters(ctypes.Structure):
            _fields_ = (
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            )

        psapi: Any = ctypes.WinDLL("psapi", use_last_error=True)  # Any: a ctypes DLL
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters),
                                               wintypes.DWORD)  # fmt: skip
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        me = self._k32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(me, ctypes.byref(counters), counters.cb):
            return False
        floor = int(counters.WorkingSetSize) + WS_FLOOR_MARGIN
        flags = QUOTA_LIMITS_HARDWS_MIN_ENABLE | QUOTA_LIMITS_HARDWS_MAX_DISABLE
        return bool(self._k32.SetProcessWorkingSetSizeEx(me, floor, 2 * floor, flags))

    def settle_gc(self) -> None:
        """Rev 2 non-blocking note: no cyclic collection on the hook thread once running."""
        gc.collect()
        gc.freeze()
        gc.disable()

    def install_hooks(
        self, keyboard: KeyboardSink, mouse: MouseSink, on_error: Callable[[], None]
    ) -> None:
        u = self._u32
        hookproc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM,
                                      wintypes.LPARAM)  # fmt: skip
        u.SetWindowsHookExW.argtypes = (ctypes.c_int, hookproc, wintypes.HINSTANCE,
                                        wintypes.DWORD)  # fmt: skip

        def call_next(code: int, wparam: int, lparam: int) -> int:
            return int(u.CallNextHookEx(None, code, wparam, lparam))

        procs = (
            (WH_MOUSE_LL, make_mouse_proc(mouse, on_error, call_next)),
            (WH_KEYBOARD_LL, make_keyboard_proc(keyboard, on_error, call_next)),
        )
        for kind, proc in procs:
            native = hookproc(proc)
            self._procs.append(native)
            hook = u.SetWindowsHookExW(kind, native, None, 0)
            if not hook:
                raise HelperStepError(f"SetWindowsHookEx failed ({ctypes.get_last_error()})")
            self._hooks.append(int(hook))

    def register_hotkey(self, mods: int, vk: int) -> None:
        if not self._u32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
            raise HelperStepError(f"RegisterHotKey failed ({ctypes.get_last_error()})")
        self._hotkey = True

    def wait(self, timeout_ms: int, parent: int, quit_event: int) -> str:
        handles = (wintypes.HANDLE * 2)(parent, quit_event)
        got = int(self._u32.MsgWaitForMultipleObjectsEx(2, handles, timeout_ms, QS_ALLINPUT,
                                                        MWMO_INPUTAVAILABLE))  # fmt: skip
        if got not in _WAKES:
            raise HelperStepError(f"MsgWaitForMultipleObjectsEx returned {got:#x}")
        return _WAKES[got]

    def pump(self) -> Iterator[str]:
        msg = wintypes.MSG()
        u = self._u32
        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                yield "hotkey"
            else:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))

    def wake(self, handle: int) -> None:
        self._k32.SetEvent(handle)

    def release(self) -> None:
        while self._hooks:
            self._u32.UnhookWindowsHookEx(self._hooks.pop())
        if self._hotkey:
            self._u32.UnregisterHotKey(None, HOTKEY_ID)
            self._hotkey = False


def main() -> int:  # pragma: no cover - the helper process's entry point, Windows only
    return run_helper(sys.argv[1:], WindowsHelperPlatform())


if __name__ == "__main__":  # pragma: no cover - `python -I -m steerai.session.hook_helper`
    sys.exit(main())
