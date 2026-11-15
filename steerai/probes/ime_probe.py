"""[V2.59]: the keyboard hook's probe key with an IME open. `probe_key` picked the key on a
machine with en-US only, so the IME was closed; this probe repeats its batches with a Chinese,
Japanese or Korean IME open in our own Notepad, and times the probe key's echo there.

It runs only where such an IME is installed (none is installed by this probe: that is a system
setting and the person's call). It keeps `probe_key`'s conditions: a fresh Notepad closed by its
own pid, the foreground checked before every batch and never taken back, key-ups with Steer's tag
only, the idle gate and an abort on any input of the person's, the text read through UIA, JSON
lines on stdout. On top of them it changes one thing, and puts it back in `finally`:
- the input locale of its own Notepad, by posting WM_INPUTLANGCHANGEREQUEST to its Edit (the
  window with the focus; DefWindowProc activates the locale). With Windows' default of one input
  method for every window, the person's current input method changes with it until restored.
- the IME's open status, by WM_IME_CONTROL IMC_SETOPENSTATUS to the Edit's default IME window.
Both are read back: a locale that did not switch, or an IME that did not open, aborts. Every IME
read and write goes through SendMessageTimeoutW with abort-if-hung, so a hung IME never holds the
probe or its restore. The first row says the person's input method is switched until the
restore; the layout before is said (`before_layout`, with the `--restore` command for it) before
the first switch. SIGTERM and SIGBREAK raise inside the probe, so the restore in `finally` runs.

Known limit: a hard kill runs no `finally`. Anything that ends the process with TerminateProcess
(Task Manager's End task, `taskkill /F`, and on Windows `os.kill` with any signal other than
CTRL_C_EVENT or CTRL_BREAK_EVENT, Python docs, os.kill) leaves the input method switched. Put it
back with `uv run python -m steerai.probes.ime_probe --restore HKL`, HKL from `before_layout`:
it starts a fresh Notepad of its own, requests the layout there, reads it back and closes it.

A key passes as in `probe_key`, judged against the baseline taken with the IME open: the text is
still empty, the caret, the IME open status, the layout and the foreground are unchanged, the key
is not left down, and the hook saw every key-up as Steer's own.

Run (sends real input): `uv run python -m steerai.probes.ime_probe [--batch 20] [--layout HKL]`.
Exit codes: 0 the probe key passed, 1 it did not, 2 aborted, 3 no IME installed, 4 the restore
failed (the locale or the IME's open status did not read back as before; said, never silent).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Final, Protocol

from steerai.probes.probe_key import (
    IDLE_S,
    IMC_GETOPENSTATUS,
    ROUND_TRIPS,
    SETTLE_S,
    Abort,
    _Desk,
    _text_reader,
)
from steerai.session.monitor import PROBE_VK

__all__ = [
    "NOT_APPLICABLE",
    "RESTORE_FAILED",
    "Abort",
    "Notepad",
    "RestoreFailed",
    "ime_layouts",
    "main",
    "parse_hkl",
    "restore_layout",
    "run",
]

IME_LANGUAGES: Final = frozenset({0x04, 0x11, 0x12})
"""winnt.h (SDK 10.0.19041.0): LANG_CHINESE 0x04, LANG_JAPANESE 0x11, LANG_KOREAN 0x12."""
WM_INPUTLANGCHANGEREQUEST: Final = 0x0050
"""Microsoft Learn, WM_INPUTLANGCHANGEREQUEST: lParam is the input locale identifier."""
WM_IME_CONTROL: Final = 0x0283
IMC_SETOPENSTATUS: Final = 0x0006
"""immdev.h (SDK 10.0.19041.0); lParam nonzero opens the IME (IMC_SETOPENSTATUS, Microsoft
Learn)."""
SMTO_ABORTIFHUNG: Final = 0x0002
SEND_TIMEOUT_MS: Final = 1000
SWITCH_S: Final = 0.5
"""The wait after each switch before it is read back; a switch not seen by then aborts."""
NOT_APPLICABLE: Final = 3
RESTORE_FAILED: Final = 4
Row = dict[str, Any]  # Any: JSON values


class RestoreFailed(Exception):  # noqa: N818 - the probe's own outcome, said as its name
    """The input locale or the IME's open status did not read back as it was before the probe.
    Raised from the restore, so it is what the run ends with even after an abort."""


SendTimeout = Callable[[int, int, int, int], tuple[bool, int]]
"""SendMessageTimeoutW with abort-if-hung and `SEND_TIMEOUT_MS`: whether the window answered, and
its answer."""


def read_open_status(send: SendTimeout, ime: int) -> int | str | None:
    """The IME's open status from its default IME window, bounded: `unanswered` when the window
    did not answer in time (a hung IME never holds the probe or its restore), None with no IME
    window."""
    if not ime:
        return None
    answered, result = send(ime, WM_IME_CONTROL, IMC_GETOPENSTATUS, 0)
    return int(result) if answered else "unanswered"


def primary_language(hkl: int) -> int:
    """The HKL's low word is its language identifier (GetKeyboardLayout, Microsoft Learn); the
    primary language is that identifier's low ten bits (winnt.h PRIMARYLANGID)."""
    return (hkl & 0xFFFF) & 0x3FF


def ime_layouts(hkls: Sequence[int]) -> list[int]:
    """The installed input locales whose language is Chinese, Japanese or Korean, in order."""
    return [h for h in hkls if primary_language(h) in IME_LANGUAGES]


def parse_hkl(text: str) -> int:
    """An input locale identifier in hex (`0x04110411` or `04110411`)."""
    try:
        value = int(text, 16)
    except ValueError:
        raise ValueError(f"not a hkl: {text!r}") from None
    if not 0 < value < 1 << 64:
        raise ValueError(f"not a hkl: {text!r}")
    return value


def verdict(baseline: Row, after: Row, *, sent: int, seen: int, left_down: bool) -> Row:
    """One key's result against the baseline taken with the IME open."""
    changed = sorted(k for k in baseline if after.get(k) != baseline[k])
    return {
        "sent": sent,
        "hook_saw": seen,
        "left_down": left_down,
        "changed": changed,
        "passes": not changed and not left_down and seen == sent,
    }


@dataclass(frozen=True)
class Notepad:
    pid: int
    hwnd: int
    close: Callable[[], None]


class Monitor(Protocol):
    def start(self) -> None: ...
    def close(self) -> None: ...
    def counts(self) -> dict[str, int]: ...


class ImeDesk(Protocol):
    def idle_s(self) -> float: ...
    def foreground(self) -> int: ...
    def edit_of(self, hwnd: int) -> int: ...
    def key_down(self, vk: int) -> bool: ...
    def state(self, hwnd: int, read_text: Callable[[], str]) -> Row: ...
    def layouts(self) -> list[int]: ...
    def request_layout(self, edit: int, hkl: int) -> None: ...
    def set_ime_open(self, edit: int, open_: bool) -> None: ...


def _person(monitor: Monitor) -> int:
    counts = monitor.counts()
    return int(counts.get("user", 0)) + int(counts.get("injected_other", 0))


def _batch(vk: int, size: int, monitor: Monitor, send_up: Callable[[int], None],
           pause: Callable[[float], None]) -> int:  # fmt: skip
    """`size` tagged key-ups of `vk`; how many the hook saw as Steer's own."""
    person = _person(monitor)
    before = int(monitor.counts().get("steer", 0))
    for _ in range(size):
        send_up(vk)
        pause(0.01)
    pause(SETTLE_S)
    if _person(monitor) != person:
        raise Abort("person_input")
    return int(monitor.counts().get("steer", 0)) - before


def _target(installed: Sequence[int], layout: int | None) -> int:
    if layout is not None:
        if layout not in installed:
            raise Abort("layout_not_installed")
        return layout
    candidates = ime_layouts(installed)
    if not candidates:
        raise Abort("no_ime_installed")
    return candidates[0]


def run(  # noqa: PLR0913 - every side of the desk injected, keyword-only
    *,
    desk: ImeDesk,
    launch: Callable[[], Notepad],
    read_text_of: Callable[[int], Callable[[], str]],
    monitor: Monitor,
    send_up: Callable[[int], None],
    echo: Callable[[int], Row],
    candidates: Sequence[int],
    size: int,
    layout: int | None,
    emit: Callable[[Row], None],
    pause: Callable[[float], None] = time.sleep,
) -> list[int]:
    """The keys among `candidates` that change nothing with the IME open, in order. Nothing is
    launched or sent unless the person is idle and an IME layout is installed; once Notepad is
    up, its input locale and the IME's open status are put back and it is closed, abort or not."""
    target = _target(desk.layouts(), layout)  # first: with no IME the probe never applies
    if desk.idle_s() < IDLE_S:
        emit({"restored": True, "switched": False})
        raise Abort("not_idle")
    emit({"header": {"probe_key_ime": "open", "input_method": "switched_until_restore"}})
    monitor.start()
    accepted: list[int] = []
    try:
        notepad = launch()
        emit({"notepad_pid": notepad.pid, "notepad_hwnd": notepad.hwnd, "layout": f"{target:#x}"})
        try:
            pause(1.0)
            edit = desk.edit_of(notepad.hwnd)
            if not edit:
                raise Abort("no_edit_control")
            read_text = read_text_of(edit)
            before = desk.state(notepad.hwnd, read_text)
            if before["foreground"] != notepad.hwnd:
                emit({"restored": True, "switched": False})
                raise Abort("notepad_not_foreground")
            was = f"{int(before['layout']):#x}"
            emit({"before_layout": was, "restore_with": f"--restore {was}"})
            try:
                accepted = _judge(
                    desk,
                    hwnd=notepad.hwnd,
                    edit=edit,
                    read_text=read_text,
                    target=target,
                    monitor=monitor,
                    send_up=send_up,
                    candidates=candidates,
                    size=size,
                    emit=emit,
                    pause=pause,
                )
                if PROBE_VK in accepted:  # one Python hook in the chain: the echo's own
                    monitor.close()
                    emit(echo(PROBE_VK))
            finally:
                _restore(
                    desk,
                    hwnd=notepad.hwnd,
                    edit=edit,
                    before=before,
                    read_text=read_text,
                    emit=emit,
                    pause=pause,
                )
        finally:
            notepad.close()  # only our own pid
    finally:
        monitor.close()
    return accepted


def _restore(  # noqa: PLR0913 - the run's state at the restore
    desk: ImeDesk,
    *,
    hwnd: int,
    edit: int,
    before: Row,
    read_text: Callable[[], str],
    emit: Callable[[Row], None],
    pause: Callable[[float], None],
) -> None:
    """Close the IME as it was and put the locale back, both always tried; read both back and
    say the result. Anything short of both as before raises `RestoreFailed` (an abort in flight
    stays its context)."""
    errors: list[str] = []
    try:
        desk.set_ime_open(edit, bool(before["ime_open"]))
    except Abort as unanswered:  # said in the row and turned into RestoreFailed below
        errors.append(str(unanswered))
    try:
        desk.request_layout(edit, int(before["layout"]))
    except OSError:  # PostMessageW refused: said in the row and turned into RestoreFailed
        errors.append("layout_request_refused")
    pause(SWITCH_S)
    after = desk.state(hwnd, read_text)
    restored = (
        not errors
        and after["layout"] == before["layout"]
        and after["ime_open"] == before["ime_open"]
    )
    emit({"restored": restored, "switched": True, "restore_errors": errors})
    if not restored:
        raise RestoreFailed


def _judge(  # noqa: PLR0913 - the run's state, passed down once
    desk: ImeDesk,
    *,
    hwnd: int,
    edit: int,
    read_text: Callable[[], str],
    target: int,
    monitor: Monitor,
    send_up: Callable[[int], None],
    candidates: Sequence[int],
    size: int,
    emit: Callable[[Row], None],
    pause: Callable[[float], None],
) -> list[int]:
    """Switch our Edit to `target` and open its IME, read the baseline back, judge each key."""
    desk.request_layout(edit, target)
    pause(SWITCH_S)
    desk.set_ime_open(edit, True)
    pause(SWITCH_S)
    baseline = desk.state(hwnd, read_text)
    emit({"baseline": baseline})
    if baseline["layout"] != target:
        raise Abort("layout_not_switched")
    if baseline["ime_open"] != 1:
        raise Abort("ime_not_open")
    if not baseline["text_empty"]:
        raise Abort("notepad_not_empty")
    accepted: list[int] = []
    for vk in candidates:
        if desk.foreground() != hwnd:
            raise Abort("foreground_left_notepad")
        seen = _batch(vk, size, monitor, send_up, pause)
        row = verdict(baseline, desk.state(hwnd, read_text), sent=size, seen=seen,
                      left_down=desk.key_down(vk))  # fmt: skip
        emit({"vk": f"{vk:#04x}", **row})
        if row["passes"]:
            accepted.append(vk)
    return accepted


class _ImeDesk(_Desk):
    """`probe_key`'s reads, plus the installed locales and the two switches of our own Edit."""

    def __init__(self) -> None:
        super().__init__()
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetKeyboardLayoutList.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
        u.GetKeyboardLayoutList.restype = ctypes.c_int
        u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.PostMessageW.restype = wintypes.BOOL
        u.SendMessageTimeoutW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        )
        u.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        self._user = u

    def layouts(self) -> list[int]:
        n = int(self._user.GetKeyboardLayoutList(0, None))
        buffer = (ctypes.c_void_p * max(n, 1))()
        got = int(self._user.GetKeyboardLayoutList(n, buffer))
        return [int(h or 0) for h in buffer[:got]]

    def request_layout(self, edit: int, hkl: int) -> None:
        if not self._user.PostMessageW(edit, WM_INPUTLANGCHANGEREQUEST, 0, hkl):
            raise OSError(ctypes.get_last_error(), "PostMessageW")

    def _send_timeout(self, hwnd: int, message: int, wparam: int, lparam: int) -> tuple[bool, int]:
        result = ctypes.c_size_t(0)
        sent = self._user.SendMessageTimeoutW(hwnd, message, wparam, lparam, SMTO_ABORTIFHUNG,
                                              SEND_TIMEOUT_MS, ctypes.byref(result))  # fmt: skip
        return bool(sent), int(result.value)

    def _ime_open(self, hwnd: int) -> int | str | None:
        """Replaces `probe_key`'s plain SendMessageW read: every IME read here is bounded, the
        baseline and the post-restore readback included."""
        return read_open_status(self._send_timeout, int(self._imm.ImmGetDefaultIMEWnd(hwnd) or 0))

    def set_ime_open(self, edit: int, open_: bool) -> None:
        ime = self._imm.ImmGetDefaultIMEWnd(edit)
        if not ime:
            raise Abort("no_ime_window")
        answered, _ = self._send_timeout(int(ime), WM_IME_CONTROL, IMC_SETOPENSTATUS, int(open_))
        if not answered:
            raise Abort("ime_control_unanswered")


def restore_layout(  # noqa: PLR0913 - every side of the desk injected, keyword-only
    *,
    desk: ImeDesk,
    launch: Callable[[], Notepad],
    read_text_of: Callable[[int], Callable[[], str]],
    hkl: int,
    emit: Callable[[Row], None],
    pause: Callable[[float], None] = time.sleep,
) -> bool:
    """`--restore`: after a hard kill, put the input locale back to `hkl` through a fresh Notepad
    of our own (never a window of the person's), read it back, close it. No input is sent."""
    if hkl not in desk.layouts():
        raise Abort("layout_not_installed")
    notepad = launch()
    try:
        pause(1.0)
        edit = desk.edit_of(notepad.hwnd)
        if not edit:
            raise Abort("no_edit_control")
        read_text = read_text_of(edit)
        if desk.state(notepad.hwnd, read_text)["foreground"] != notepad.hwnd:
            raise Abort("notepad_not_foreground")
        desk.request_layout(edit, hkl)
        pause(SWITCH_S)
        now = int(desk.state(notepad.hwnd, read_text)["layout"])
        emit({"restored": now == hkl, "layout": f"{now:#x}"})
        return now == hkl
    finally:
        notepad.close()


@contextmanager
def raising_on_termination() -> Iterator[None]:
    """SIGTERM and SIGBREAK (where the platform has them) raise `Abort("killed")` on the main
    thread, so every `finally` runs; the handlers before are put back after."""

    def killed(_signum: int, _frame: object) -> None:
        raise Abort("killed")

    numbers = [getattr(signal, n) for n in ("SIGTERM", "SIGBREAK") if hasattr(signal, n)]
    previous = {n: signal.signal(n, killed) for n in numbers}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


def _launch_notepad() -> Notepad:  # pragma: no cover - live, Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only

    process = subprocess.Popen(["notepad.exe"])  # noqa: S607 - Windows' own Notepad, no shell

    def close() -> None:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()

    try:
        hwnd = wait_window_for_pid(process.pid, timeout_s=15.0, process=process).hwnd
    except BaseException:
        close()
        raise
    return Notepad(pid=process.pid, hwnd=hwnd, close=close)


def _echo(vk: int) -> Row:  # pragma: no cover - live, Windows only
    """The probe key's round trip with the IME open, idle load, on its own per-hook monitor."""
    from steerai.probes.probe_key import round_trip  # noqa: PLC0415 - Windows only
    from steerai.session.monitor import (  # noqa: PLC0415 - Windows hooks
        HookMonitor,
        probe_deadline_s,
        read_hook_timeout_ms,
    )

    deadline_ms = round(probe_deadline_s(read_hook_timeout_ms()) * 1000, 3)
    phase = HookMonitor(per_hook=True, probe_vk=vk)
    phase.start()
    try:
        return round_trip(vk, ROUND_TRIPS, phase, load="none", deadline_ms=deadline_ms)
    finally:
        phase.close()


def _live(  # pragma: no cover - live, Windows only
    layout: int | None, size: int, emit: Callable[[Row], None]
) -> list[int]:
    """The probe on this desk: the probe key first, then every other unassigned key."""
    from steerai.probes.win_input import key_up_probe  # noqa: PLC0415 - Windows only
    from steerai.session.monitor import UNASSIGNED_VKS, HookMonitor  # noqa: PLC0415

    keys = [PROBE_VK, *sorted(UNASSIGNED_VKS - {PROBE_VK})]
    return run(desk=_ImeDesk(), launch=_launch_notepad, read_text_of=_text_reader,
               monitor=HookMonitor(), send_up=key_up_probe, echo=_echo, candidates=keys, size=size,
               layout=layout, emit=emit)  # fmt: skip


def _live_restore(hkl: int, emit: Callable[[Row], None]) -> bool:  # pragma: no cover - live
    return restore_layout(desk=_ImeDesk(), launch=_launch_notepad, read_text_of=_text_reader,
                          hkl=hkl, emit=emit)  # fmt: skip


def main(
    argv: Sequence[str] | None = None,
    *,
    probe: Callable[[int | None, int, Callable[[Row], None]], list[int]] = _live,
    restore: Callable[[int, Callable[[Row], None]], bool] = _live_restore,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m steerai.probes.ime_probe")
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--layout", type=parse_hkl, default=None)
    parser.add_argument("--restore", type=parse_hkl, default=None, metavar="HKL")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    def emit(row: Row) -> None:
        sys.stdout.write(json.dumps({"probe": "ime_probe", **row}) + "\n")
        sys.stdout.flush()

    try:
        with raising_on_termination():
            if args.restore is not None:
                return 0 if restore(args.restore, emit) else RESTORE_FAILED
            accepted = probe(args.layout, args.batch, emit)
    except RestoreFailed:
        emit({"aborted": "restore_failed"})
        return RESTORE_FAILED
    except Abort as stop:
        emit({"aborted": str(stop)})
        return NOT_APPLICABLE if str(stop) == "no_ime_installed" else 2
    passes = PROBE_VK in accepted
    emit({"accepted": [f"{vk:#04x}" for vk in accepted], "probe_vk_passes": passes})
    return 0 if passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
