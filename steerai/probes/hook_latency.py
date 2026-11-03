"""Hook-latency probe (design 5.1 [v1.4] decision: low-level hook versus Raw Input).

A `WH_KEYBOARD_LL` / `WH_MOUSE_LL` hook procedure runs on the installing thread through a
ctypes callback, which needs the GIL. While the main thread builds a snapshot or runs pure
Python, every keystroke on the machine waits for that callback, and a callback slower than
`HKCU\\Control Panel\\Desktop\\LowLevelHooksTimeout` gets the hook silently removed
(learn.microsoft.com, LowLevelKeyboardProc, read 2026-11-04). This probe measures, under three
main-thread loads (idle, pure-Python GIL hold, `observe()` on a Chrome window), the latency from
a tagged `SendInput` on a sibling thread to the callback's entry, and the callback body itself.

    uv run python -m steerai.probes.hook_latency [--events 200] [--interval-ms 50]
        [--no-chrome] [--chrome path] [--rows 400] [--out r.json]
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steerai.probes.win_input import (
    STEER_EXTRA_INFO,
    ULONG_PTR,
    _to_absolute,
    _user32,
    build_mouse_input,
    cursor_pos,
)

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
_PAGE = Path(__file__).resolve().parent / "pages" / "inbox.html"


def classify(*, flags: int, extra: int, mouse: bool) -> str:
    """Steer's tag proves Steer; the injected bit alone proves only "not the keyboard driver"."""
    if extra == STEER_EXTRA_INFO:
        return "steer"
    injected = LLMHF_INJECTED if mouse else LLKHF_INJECTED
    return "injected_other" if flags & injected else "user"


@dataclass(frozen=True, slots=True)
class Sample:
    sent_ns: int
    entered_ns: int
    body_ns: int

    @property
    def latency_ns(self) -> int:
        return self.entered_ns - self.sent_ns


@dataclass
class Ledger:
    """Injections and callbacks matched in order: the probe sends one tagged event at a time."""

    pending: deque[int] = field(default_factory=deque)
    samples: list[Sample] = field(default_factory=list)
    bodies_ns: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    unmatched: int = 0

    def sent(self, sent_ns: int) -> None:
        self.pending.append(sent_ns)

    def seen(self, *, kind: str, entered_ns: int, body_ns: int) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.bodies_ns.append(body_ns)
        if kind != "steer":
            return
        if not self.pending:
            self.unmatched += 1
            return
        self.samples.append(Sample(self.pending.popleft(), entered_ns, body_ns))


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarise(ledger: Ledger, *, timeout_ms: int | None) -> dict[str, Any]:
    latencies = [s.latency_ns / 1e6 for s in ledger.samples]
    bodies = [b / 1e6 for b in ledger.bodies_ns]
    over = 0 if timeout_ms is None else sum(1 for value in bodies if value > timeout_ms)
    return {
        "samples": len(latencies),
        "unmatched": ledger.unmatched,
        "counts": dict(ledger.counts),
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
        },
        "body_ms": {
            "p50": _percentile(bodies, 0.5),
            "p95": _percentile(bodies, 0.95),
            "max": max(bodies) if bodies else None,
        },
        "timeout_ms": timeout_ms,
        "over_timeout": over,
    }


# ---- the hook thread (Windows) -------------------------------------------------------------


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class HookThread:
    """Both low-level hooks on one dedicated thread with a `GetMessageW` loop; the callback
    reads two fields, records kind and times, and passes the event on."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: str | None = None
        self._procs: list[Any] = []  # the WINFUNCTYPE objects must outlive the hooks

    def start(self) -> None:
        if sys.platform != "win32":
            raise OSError("low-level hooks require Windows")
        self._thread = threading.Thread(target=self._run, name="steer-hook-probe", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise OSError("hook thread did not start")
        if self._error is not None:
            raise OSError(self._error)

    def stop(self) -> None:
        if self._thread is None:
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int,
            _HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        )
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = (
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._thread_id = int(kernel32.GetCurrentThreadId())
        ledger = self._ledger

        def make(mouse: bool) -> Any:
            struct = MSLLHOOKSTRUCT if mouse else KBDLLHOOKSTRUCT

            def proc(code: int, wparam: int, lparam: int) -> int:
                entered = time.perf_counter_ns()
                if code == HC_ACTION:
                    info = ctypes.cast(lparam, ctypes.POINTER(struct)).contents
                    kind = classify(flags=int(info.flags), extra=int(info.dwExtraInfo), mouse=mouse)
                    body = time.perf_counter_ns() - entered
                    ledger.seen(kind=kind, entered_ns=entered, body_ns=body)
                return int(user32.CallNextHookEx(None, code, wparam, lparam))

            return _HOOKPROC(proc)

        hooks: list[Any] = []
        try:
            for mouse, kind in ((True, WH_MOUSE_LL), (False, WH_KEYBOARD_LL)):
                proc = make(mouse)
                self._procs.append(proc)
                hook = user32.SetWindowsHookExW(kind, proc, None, 0)
                if not hook:
                    self._error = f"SetWindowsHookEx failed: {ctypes.get_last_error()}"
                    return
                hooks.append(hook)
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for hook in hooks:
                user32.UnhookWindowsHookEx(hook)
            self._ready.set()


def inject_probe() -> None:
    """One tagged absolute move to the cursor's current position. Measured 2026-11-04: a
    zero-delta relative move and a zero-delta wheel never reach a low-level hook; an absolute
    move to the same position does, and the cursor does not move."""
    user32 = _user32()
    x, y = cursor_pos()
    ax, ay = _to_absolute(user32, x, y)
    record = build_mouse_input(
        dx=ax, dy=ay, flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    )
    if user32.SendInput(1, ctypes.byref(record), ctypes.sizeof(record)) != 1:
        raise OSError(f"SendInput failed: {ctypes.get_last_error()}")


def low_level_hooks_timeout_ms() -> int | None:
    """The machine's `LowLevelHooksTimeout`, or None when the registry has no value (the OS
    default is then in force; the docs do not state it)."""
    import winreg  # noqa: PLC0415 - Windows only

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
            value, _ = winreg.QueryValueEx(key, "LowLevelHooksTimeout")
    except OSError:
        return None
    return int(value) if isinstance(value, int) else None


# ---- loads and the run -----------------------------------------------------------------------


def _drive(ledger: Ledger, *, events: int, interval_ms: float) -> None:
    for _ in range(events):
        # the send time goes in before the call: the callback can run before SendInput returns
        ledger.sent(time.perf_counter_ns())
        inject_probe()
        time.sleep(interval_ms / 1000)
    time.sleep(0.5)  # let the last callbacks land


def run_case(
    name: str, load: Callable[[threading.Event], None] | None, *, events: int, interval_ms: float
) -> dict[str, Any]:
    ledger = Ledger()
    hooks = HookThread(ledger)
    hooks.start()
    stop = threading.Event()
    driver = threading.Thread(
        target=_drive, args=(ledger,), kwargs={"events": events, "interval_ms": interval_ms}
    )
    try:
        driver.start()
        if load is not None:
            load(stop)
            while driver.is_alive():
                load(stop)
        driver.join()
    finally:
        stop.set()
        hooks.stop()
    report = summarise(ledger, timeout_ms=low_level_hooks_timeout_ms())
    report["case"] = name
    return report


def run_all(
    *, events: int, interval_ms: float, chrome: Path | None, rows: int = 0
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    cases.append(run_case("idle", None, events=events, interval_ms=interval_ms))

    def gil(stop: threading.Event) -> None:
        for _ in range(20):
            if stop.is_set():
                return
            total = 0
            for i in range(50_000):
                total += i * i

    cases.append(run_case("gil_hold", gil, events=events, interval_ms=interval_ms))
    if chrome is not None and chrome.is_file():
        from steerai.probes.chrome_a11y import launch_chrome  # noqa: PLC0415
        from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
        from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
        from steerai.world.model import WorldModel  # noqa: PLC0415

        profile = Path(tempfile.mkdtemp(prefix="steer-hook-"))
        url = _PAGE.as_uri() + (f"?rows={rows}" if rows else "")
        proc = launch_chrome(chrome, profile, 9340, url, window_size=(1000, 640))
        try:
            window = wait_window_for_pid(proc.pid, title_contains="Google Chrome")
            model = WorldModel(UiaClient())
            model.observe(window.hwnd, cap_s=5.0)
            counted = model.observe(window.hwnd).node_count

            def observing(stop: threading.Event) -> None:
                if not stop.is_set():
                    model.observe(window.hwnd, stable_window=1)

            report = run_case("observe_chrome", observing, events=events, interval_ms=interval_ms)
            report["node_count"] = counted
            cases.append(report)
        finally:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            shutil.rmtree(profile, ignore_errors=True)
    return {
        "events_per_case": events,
        "interval_ms": interval_ms,
        "switch_interval_s": sys.getswitchinterval(),
        "cases": cases,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=200)
    parser.add_argument("--interval-ms", type=float, default=50.0)
    parser.add_argument("--no-chrome", action="store_true")
    parser.add_argument("--chrome", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--rows", type=int, default=400, help="extra chat rows on the page (0 = the plain page)"
    )
    args = parser.parse_args(argv)
    from steerai.probes.chrome_a11y import CHROME_DEFAULT  # noqa: PLC0415

    chrome = None if args.no_chrome else Path(args.chrome) if args.chrome else CHROME_DEFAULT
    result = run_all(
        events=args.events, interval_ms=args.interval_ms, chrome=chrome, rows=args.rows
    )
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
