"""[V2.57] LW-01: pick the keyboard hook's probe key by probe. Each unassigned virtual key is sent
as tagged key-ups into our own, empty Notepad; a key passes when nothing changes.

Conditions (the coordinator's, 2026-11-15):
1. A fresh Notepad: its pid and hwnd recorded; only that pid is closed at the end, abort or not.
2. Before every batch the foreground must be our Notepad, else abort with nothing sent; never
   refocus around the person.
3. Key-ups only, with Steer's tag: no key-down, no click, no cursor move.
4. Idle gate: no input of the person's for 2 s before the start; abort on any untagged input
   (a hook monitor classifies every event by its tag).
5. Each batch reads the text through UIA (ValuePattern) and the caret position.
6. Output is JSON lines on stdout only; Notepad starts empty and must stay empty.

A key passes when, after its batch: the text is still empty, the caret, the IME open status,
the keyboard layout and the foreground are unchanged, the key is not left down, and the
keyboard hook saw every key-up as Steer's own.

Run (sends real input): `uv run python -m steerai.probes.probe_key [--batch 20]`.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from ctypes import wintypes
from typing import Any

IDLE_S = 2.0
SETTLE_S = 0.2
ROUND_TRIPS = 250
LOADS = ("none", "spin", "json")
JSON_PAYLOAD_BYTES = 200_000
JSON_PAUSE_S = 0.002
WM_IME_CONTROL = 0x0283
IMC_GETOPENSTATUS = 0x0005
UIA_VALUE_PATTERN = 10002
Row = dict[str, Any]  # Any: JSON values


class Abort(Exception):  # noqa: N818 - the probe's own stop, said as its reason
    pass


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    )


class _LastInput(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


class _Desk:
    """The Win32 reads the probe makes; none of them touches a window."""

    def __init__(self) -> None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = wintypes.HWND
        u.FindWindowExW.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        )
        u.FindWindowExW.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
        u.GetKeyboardLayout.restype = ctypes.c_void_p
        u.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo))
        u.GetLastInputInfo.argtypes = (ctypes.POINTER(_LastInput),)
        imm = ctypes.WinDLL("imm32")
        imm.ImmGetDefaultIMEWnd.argtypes = (wintypes.HWND,)
        imm.ImmGetDefaultIMEWnd.restype = wintypes.HWND
        k = ctypes.WinDLL("kernel32")
        k.GetTickCount.restype = wintypes.DWORD
        self._u, self._imm, self._k = u, imm, k

    def idle_s(self) -> float:
        last = _LastInput()
        last.cbSize = ctypes.sizeof(_LastInput)
        self._u.GetLastInputInfo(ctypes.byref(last))
        return float((int(self._k.GetTickCount()) - int(last.dwTime)) & 0xFFFF_FFFF) / 1000

    def foreground(self) -> int:
        return int(self._u.GetForegroundWindow() or 0)

    def edit_of(self, hwnd: int) -> int:
        return int(self._u.FindWindowExW(hwnd, None, "Edit", None) or 0)

    def key_down(self, vk: int) -> bool:
        return bool(int(self._u.GetAsyncKeyState(vk)) & 0x8000)

    def state(self, hwnd: int, read_text: Callable[[], str]) -> Row:
        """Everything a stray key could change."""
        thread = int(self._u.GetWindowThreadProcessId(hwnd, None))
        info = _GuiThreadInfo()
        info.cbSize = ctypes.sizeof(_GuiThreadInfo)
        self._u.GetGUIThreadInfo(thread, ctypes.byref(info))
        ime = self._imm.ImmGetDefaultIMEWnd(hwnd)
        rect = info.rcCaret
        return {
            "foreground": self.foreground(),
            "text_empty": read_text() == "",
            "caret": [rect.left, rect.top, rect.right, rect.bottom],
            "caret_hwnd": int(info.hwndCaret or 0),
            "ime_open": int(self._u.SendMessageW(ime, WM_IME_CONTROL, IMC_GETOPENSTATUS, 0))
            if ime
            else None,
            "layout": int(self._u.GetKeyboardLayout(thread) or 0),
        }


def _text_reader(edit: int) -> Callable[[], str]:
    """The Edit control's text through UIA's ValuePattern."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only

    client = UiaClient()
    uia, module = client._uia, client._uia_module

    def read() -> str:
        element = uia.ElementFromHandle(edit)
        pattern = element.GetCurrentPattern(UIA_VALUE_PATTERN)
        return str(pattern.QueryInterface(module.IUIAutomationValuePattern).CurrentValue)

    return read


def _steer_events(monitor: Any) -> int:  # Any: HookMonitor
    """Own events seen by either hook; only key-ups are sent, so each is the keyboard's."""
    return int(monitor.counts().get("steer", 0))


def _person_events(monitor: Any) -> int:  # Any: HookMonitor
    counts = monitor.counts()
    return int(counts.get("user", 0)) + int(counts.get("injected_other", 0))


def _batch(vk: int, size: int, monitor: Any) -> int:  # Any: HookMonitor
    """`size` tagged key-ups of `vk`; how many the keyboard hook saw as Steer's own."""
    from steerai.probes.win_input import key_up_probe  # noqa: PLC0415 - Windows only

    person = _person_events(monitor)
    before = monitor.counts().get("steer", 0)
    for _ in range(size):
        key_up_probe(vk)
        time.sleep(0.01)
    time.sleep(SETTLE_S)
    if _person_events(monitor) != person:
        raise Abort("person_input")
    return int(monitor.counts().get("steer", 0)) - int(before)


def json_payload() -> str:
    """About 200 KB of JSON shaped like a listing, roughly what a decide worker parses."""
    rows: list[dict[str, object]] = []
    text = ""
    while len(text) < JSON_PAYLOAD_BYTES:
        i = len(rows)
        rows.append({"id": i, "role": "Button", "name": f"Item {i} " + "x" * 40,
                     "box": [i, i, i + 80, i + 20], "caps": ["toggle:off"]})  # fmt: skip
        if i % 100 == 0:
            text = json.dumps({"rows": rows})
    return json.dumps({"rows": rows})


@contextmanager
def load_running(load: str) -> Iterator[Callable[[], int]]:
    """A Python thread loading the GIL while the round trips run: `none`; `spin`, a tight
    loop; or `json`, json.loads of `json_payload()` with short sleeps (as a decide worker
    parses a reply). Yields how much work it did."""
    if load not in LOADS:
        raise ValueError(f"unknown load {load!r}")
    stop = threading.Event()
    work = [0]

    def spin() -> None:
        while not stop.is_set():
            work[0] += 1

    def parse() -> None:
        payload = json_payload()
        while not stop.is_set():
            json.loads(payload)
            work[0] += 1
            time.sleep(JSON_PAUSE_S)

    thread = None
    if load != "none":
        thread = threading.Thread(target=spin if load == "spin" else parse, daemon=True)
        thread.start()
    try:
        yield lambda: work[0]
    finally:
        stop.set()
        if thread is not None:
            thread.join()


def round_trip(vk: int, n: int, monitor: Any, *, load: str) -> Row:  # Any: HookMonitor
    """#364 P2: `n` tagged key-ups of `vk`, each timed from the send until the keyboard hook
    has counted it, under `load`; next to it, the hook's own latency (event tick to callback
    entry) from the monitor's histogram. Key-ups only; nothing else is sent."""
    from steerai.probes.win_input import key_up_probe  # noqa: PLC0415 - Windows only

    samples: list[float] = []
    with load_running(load):
        for _ in range(n):
            person = _person_events(monitor)
            before = _steer_events(monitor)
            started = time.perf_counter()
            key_up_probe(vk)
            while _steer_events(monitor) == before:
                if time.perf_counter() - started > 2.0:
                    raise Abort("probe_not_seen")
                time.sleep(0.0001)
            samples.append((time.perf_counter() - started) * 1000)
            if _person_events(monitor) != person:
                raise Abort("person_input")
            time.sleep(0.005)
    samples.sort()
    return {
        "round_trip": load,
        "n": n,
        "p50_ms": round(samples[(n - 1) // 2], 3),
        "p95_ms": round(samples[min(n - 1, int(0.95 * n))], 3),
        "max_ms": round(samples[-1], 3),
        "hook_events": monitor.latency()[0],
        "hook_p50_ms": monitor.latency_at(0.5),
        "hook_p95_ms": monitor.latency_at(0.95),
        "hook_max_ms": monitor.latency_at(1.0),
    }


def _phases(vk: int, desk: _Desk, hwnd: int, emit: Callable[[Row], None]) -> None:
    """One round-trip phase per load, each with its own per-hook monitor timing itself."""
    from steerai.session.monitor import HookMonitor  # noqa: PLC0415 - Windows hooks

    for load in LOADS:
        if desk.foreground() != hwnd:
            raise Abort("foreground_left_notepad")
        phase = HookMonitor(per_hook=True, probe_vk=vk)
        phase.start()
        try:
            emit(round_trip(vk, ROUND_TRIPS, phase, load=load))
        finally:
            phase.close()


def run(candidates: Sequence[int], size: int, emit: Callable[[Row], None]) -> list[int]:
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only
    from steerai.session.monitor import HookMonitor  # noqa: PLC0415 - Windows hooks

    desk = _Desk()
    if desk.idle_s() < IDLE_S:
        raise Abort("not_idle")
    monitor = HookMonitor()
    monitor.start()
    notepad = subprocess.Popen(["notepad.exe"])  # noqa: S607 - Windows' own Notepad, no shell
    accepted: list[int] = []
    try:
        hwnd = wait_window_for_pid(notepad.pid, timeout_s=15.0, process=notepad).hwnd
        emit({"notepad_pid": notepad.pid, "notepad_hwnd": hwnd})
        time.sleep(1.0)
        edit = desk.edit_of(hwnd)
        if not edit:
            raise Abort("no_edit_control")
        read_text = _text_reader(edit)
        baseline = desk.state(hwnd, read_text)
        emit({"baseline": baseline})
        if baseline["foreground"] != hwnd:
            raise Abort("notepad_not_foreground")
        if not baseline["text_empty"]:
            raise Abort("notepad_not_empty")
        for vk in candidates:
            if desk.foreground() != hwnd:
                raise Abort("foreground_left_notepad")
            seen = _batch(vk, size, monitor)
            after = desk.state(hwnd, read_text)
            changed = sorted(k for k in baseline if after[k] != baseline[k])
            down = desk.key_down(vk)
            ok = not changed and not down and seen == size
            emit({"vk": f"{vk:#04x}", "sent": size, "hook_saw": seen, "left_down": down,
                  "changed": changed, "passes": ok})  # fmt: skip
            if ok:
                accepted.append(vk)
        if accepted:  # #364 P2: the chosen key's round trip and hook latency, per load
            monitor.close()  # one Python hook in the chain: each phase's own, timing itself
            _phases(accepted[0], desk, hwnd, emit)
            after = desk.state(hwnd, read_text)
            moved = sorted(k for k in baseline if after[k] != baseline[k])
            emit({"after_round_trips_changed": moved})
    finally:
        notepad.terminate()  # only our own pid
        try:
            notepad.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            notepad.kill()
        monitor.close()
    return accepted


def main(argv: Sequence[str] | None = None) -> int:
    from steerai.session.monitor import UNASSIGNED_VKS  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m steerai.probes.probe_key")
    parser.add_argument("--batch", type=int, default=20)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    def emit(row: Row) -> None:
        sys.stdout.write(json.dumps({"probe": "probe_key", **row}) + "\n")
        sys.stdout.flush()

    try:
        accepted = run(sorted(UNASSIGNED_VKS), args.batch, emit)
    except Abort as stop:
        emit({"aborted": str(stop)})
        return 2
    first = f"{accepted[0]:#04x}" if accepted else None
    emit({"accepted": [f"{vk:#04x}" for vk in accepted], "first": first})
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
