"""The launcher and overlay window (design 5.1 asks surface; packet launcher-2026-11-04,
schema gate items 1-6 folded in).

One Tk window on the main thread: a goal entry, a target-window picker, a Run button, a log pane
and an ask panel. The runner runs on **one persistent worker thread** for the process's lifetime
(COM is initialised on the thread that first imports comtypes, so tasks are queued to that one
thread and `steerai.app` never imports comtypes on the main thread); the overlay's broker is the
runner's confirmer and the UI drains the runner's output and the pending ask every `DRAIN_MS`.
While a task runs the window is no-activate and topmost (`WS_EX_NOACTIVATE` through
`SetWindowLongPtrW`, topmost through `SetWindowPos`, 5.1 [v0.4]) so a button click never takes
foreground from the target; the switch happens synchronously in the Run handler before the
worker is signalled. Before a task it is a normal window, because the goal entry needs keyboard
focus. One task at a time. **Closing the window is the kill**: the monitor the runner starts is
wrapped so `kill_requested()` also reads the UI's flag; close sets it, joins the worker with a
bounded timeout, then destroys the root.
"""

from __future__ import annotations

import ctypes
import io
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import ttk
from typing import Any

from steerai.app.broker import Answer, AskBroker, KillSource, PendingAsk
from steerai.orchestrator.gate import Ask
from steerai.probes.windows import WindowInfo
from steerai.run import Deps, Options, parse_args, run

DRAIN_MS = 50
CLOSE_JOIN_S = 5.0
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
GWLP_WNDPROC = -4
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3
_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_longlong
)


class QueueWriter(io.TextIOBase):
    """A text stream whose lines land in a queue the UI drains."""

    def __init__(self, lines: queue.Queue[str]) -> None:
        super().__init__()
        self._lines = lines
        self._partial = ""

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, s: str) -> int:
        self._partial += s
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._lines.put(line)
        return len(s)

    def flush(self) -> None:
        if self._partial:
            self._lines.put(self._partial)
            self._partial = ""


class BrokerConfirmer:
    """The runner's confirmer: every ask goes to the broker, the UI answers with buttons."""

    def __init__(self, broker: AskBroker) -> None:
        self._broker = broker

    def ask(self, ask: Ask) -> str:
        return self._broker.confirm(ask)

    def ask_go(self, go_id: str) -> str:
        return self._broker.go(go_id)


class ClosableMonitor:
    """The runner's monitor with the UI's kill flag OR-ed in: closing the window kills."""

    def __init__(self, inner: Any, closed: threading.Event) -> None:
        self._inner = inner
        self._closed = closed

    def kill_requested(self) -> bool:
        return self._closed.is_set() or bool(self._inner.kill_requested())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def toplevel_hwnd(widget: tk.Misc) -> int:
    """The Win32 top-level window of a Tk root (its client window's parent)."""
    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = (ctypes.c_void_p,)
    return int(user32.GetParent(widget.winfo_id()) or widget.winfo_id())


def task_ex_style(hwnd: int, *, task: bool) -> int:
    """Set (task) or clear the no-activate bit through the ex-style and the topmost state
    through `SetWindowPos` (the documented way for `WS_EX_TOPMOST`); returns the new style."""
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    user32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
    user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    user32.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong)
    current = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
    wanted = (current | WS_EX_NOACTIVATE) if task else (current & ~WS_EX_NOACTIVATE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, wanted)
    user32.SetWindowPos(
        ctypes.c_void_p(hwnd),
        ctypes.c_void_p(HWND_TOPMOST if task else HWND_NOTOPMOST),
        0,
        0,
        0,
        0,
        SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )
    return int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))


class NoActivate:
    """A window-procedure subclass that answers `WM_MOUSEACTIVATE` with `MA_NOACTIVATE`: the
    ex-style alone does not stop Tk from activating on a click (measured 2026-11-04), the
    message does. Installed for a task, removed after; keeps its callback alive."""

    def __init__(self, hwnd: int) -> None:
        self._hwnd = hwnd
        user32 = ctypes.windll.user32
        user32.SetWindowLongPtrW.restype = ctypes.c_longlong
        user32.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong)
        user32.CallWindowProcW.restype = ctypes.c_longlong
        user32.CallWindowProcW.argtypes = (
            ctypes.c_longlong,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_ulonglong,
            ctypes.c_longlong,
        )
        self._previous = 0
        self._proc = _WNDPROC(self._handle)

    def _handle(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        return int(
            ctypes.windll.user32.CallWindowProcW(self._previous, hwnd, message, wparam, lparam)
        )

    def install(self) -> None:
        user32 = ctypes.windll.user32
        self._previous = int(
            user32.SetWindowLongPtrW(
                self._hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p).value or 0
            )
        )

    def remove(self) -> None:
        if self._previous:
            ctypes.windll.user32.SetWindowLongPtrW(self._hwnd, GWLP_WNDPROC, self._previous)
            self._previous = 0


class RunnerThread:
    """The one worker thread tasks are queued to (COM lives there)."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[Callable[[], None]] = queue.Queue()
        self._busy = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="steer-runner", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            self._busy.set()
            try:
                job()
            finally:
                self._busy.clear()

    def busy(self) -> bool:
        return self._busy.is_set() or not self._jobs.empty()

    def submit(self, job: Callable[[], None]) -> None:
        self._jobs.put(job)

    def wait_idle(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while self.busy():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True


class Overlay:
    def __init__(  # noqa: PLR0913 - every collaborator is injected so the window runs in tests
        self,
        *,
        root: tk.Tk,
        deps: Deps,
        list_windows: Callable[[], Sequence[WindowInfo]],
        exclusions: Callable[[], tuple[set[int], set[int]]],
        ledger_dir: Path,
        trace_dir: Path,
        runner: Callable[..., int] = run,
        worker: RunnerThread | None = None,
    ) -> None:
        self.root = root
        self._deps = deps
        self._list_windows = list_windows
        self._exclusions = exclusions
        self._ledger_dir = ledger_dir
        self._trace_dir = trace_dir
        self._runner = runner
        self._worker = worker or RunnerThread()
        self._lines: queue.Queue[str] = queue.Queue()
        self.broker = AskBroker()
        self.closed = threading.Event()
        self._windows: list[WindowInfo] = []
        self._shown: PendingAsk | None = None
        self._task_open = False
        self._no_activate: NoActivate | None = None
        self.exit_code: int | None = None
        self._build()
        self.refresh_windows()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(DRAIN_MS, self._tick)

    # ---- widgets -----------------------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.title("Steer")
        root.geometry("560x520")
        frame = ttk.Frame(root, padding=8)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Goal").grid(row=0, column=0, sticky="w")
        self.goal = ttk.Entry(frame, width=60)
        self.goal.grid(row=0, column=1, columnspan=2, sticky="ew", pady=2)
        ttk.Label(frame, text="Window").grid(row=1, column=0, sticky="w")
        self.window_box = ttk.Combobox(frame, state="readonly", width=56)
        self.window_box.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Button(frame, text="Refresh", command=self.refresh_windows).grid(row=1, column=2)
        self.run_button = ttk.Button(frame, text="Run", command=self.run_clicked)
        self.run_button.grid(row=2, column=1, sticky="w", pady=4)
        self.status = ttk.Label(frame, text="idle")
        self.status.grid(row=2, column=2, sticky="e")
        self.ask_frame = ttk.LabelFrame(frame, text="Ask", padding=6)
        self.ask_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=4)
        self.ask_text = tk.Text(self.ask_frame, height=7, width=66, wrap="word", state="disabled")
        self.ask_text.pack(fill="x")
        self.ask_buttons = ttk.Frame(self.ask_frame)
        self.ask_buttons.pack(fill="x", pady=4)
        self.log = tk.Text(frame, height=14, width=66, wrap="none", state="disabled")
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(4, weight=1)
        frame.columnconfigure(1, weight=1)

    def refresh_windows(self) -> None:
        hidden_hwnds, hidden_pids = self._exclusions()
        self._windows = [
            w
            for w in self._list_windows()
            if w.hwnd not in hidden_hwnds and w.pid not in hidden_pids and not w.minimized
        ]
        self.window_box["values"] = [f"{w.title}  [0x{w.hwnd:x}]" for w in self._windows]
        self.window_box.set("")  # nothing preselected: the person picks the target

    def selected_window(self) -> WindowInfo | None:
        index = self.window_box.current()
        if index < 0 or index >= len(self._windows):
            return None
        return self._windows[index]

    # ---- running -----------------------------------------------------------------------------

    def running(self) -> bool:
        return self._task_open

    def options_for(self, goal: str, hwnd: int) -> Options:
        task_id = f"app-{os.getpid()}-{int(time.time() * 1000)}"
        argv = [
            "--goal",
            goal,
            "--hwnd",
            f"0x{hwnd:x}",
            "--ledger-dir",
            str(self._ledger_dir),
            "--trace",
            str(self._trace_dir / f"{task_id}.jsonl"),
            "--task-id",
            task_id,
        ]
        return parse_args(argv)

    def run_clicked(self) -> None:
        if self.running():
            self._log("a task is already running (one at a time)")
            return
        goal = self.goal.get().strip()
        window = self.selected_window()
        if not goal or window is None:
            self._log("a goal and a target window are needed")
            return
        try:
            options = self.options_for(goal, window.hwnd)
        except ValueError as exc:
            self._log(f"usage: {exc}")
            return
        self.start(options)

    def start(self, options: Options) -> None:
        """Switch the window to task mode first (synchronously), then queue the run."""
        if self.running():
            raise RuntimeError("one task at a time")
        self.exit_code = None
        self._task_open = True
        self.closed.clear()
        self._set_task_mode(True)
        self.status["text"] = "running"
        self.run_button["state"] = "disabled"
        deps = self._deps.replace(
            make_confirmer=self._make_confirmer, start_monitor=self._start_monitor
        )
        writer = QueueWriter(self._lines)

        def work() -> None:
            try:
                code = self._runner(
                    options, deps, stdin=io.StringIO(), stdout=writer, stderr=writer
                )
            except Exception as exc:  # noqa: BLE001 - the thread must report, not die silently
                self._lines.put(f"internal error: {type(exc).__name__}")
                code = 70
            finally:
                writer.flush()
            self.exit_code = code

        self._worker.submit(work)

    def _make_confirmer(
        self, _stdin: Any, _stderr: Any, monitor: KillSource | None
    ) -> BrokerConfirmer:
        self.broker = AskBroker(monitor=monitor)
        return BrokerConfirmer(self.broker)

    def _start_monitor(self, options: Options) -> Any:
        start = self._deps.start_monitor
        if start is None:
            return ClosableMonitor(_NoMonitor(), self.closed)
        return ClosableMonitor(start(options), self.closed)

    def _set_task_mode(self, task: bool) -> None:
        if sys.platform != "win32":
            return
        self.root.update_idletasks()
        hwnd = toplevel_hwnd(self.root)
        task_ex_style(hwnd, task=task)
        if task and self._no_activate is None:
            self._no_activate = NoActivate(hwnd)
            self._no_activate.install()
        elif not task and self._no_activate is not None:
            self._no_activate.remove()
            self._no_activate = None

    def close(self) -> None:
        """The window's close button: the kill, a bounded join, then the root goes."""
        self.closed.set()
        self._worker.wait_idle(CLOSE_JOIN_S)
        self.root.destroy()

    # ---- the UI tick -------------------------------------------------------------------------

    def _tick(self) -> None:
        self.drain()
        self.root.after(DRAIN_MS, self._tick)

    def drain(self) -> None:
        while True:
            try:
                self._log(self._lines.get_nowait())
            except queue.Empty:
                break
        pending = self.broker.pending() if self._task_open else None
        if pending != self._shown:
            self._shown = pending
            self._show_ask(pending)
        if self._task_open and self.exit_code is not None and not self._worker.busy():
            self._finish()

    def _finish(self) -> None:
        self._task_open = False
        self._set_task_mode(False)
        self.status["text"] = f"exit {self.exit_code}"
        self.run_button["state"] = "normal"
        self._shown = None
        self._show_ask(None)

    def _show_ask(self, pending: PendingAsk | None) -> None:
        for child in self.ask_buttons.winfo_children():
            child.destroy()
        self.ask_text["state"] = "normal"
        self.ask_text.delete("1.0", "end")
        if pending is None:
            self.ask_text["state"] = "disabled"
            return
        decision_id = pending.decision_id  # captured now: a later ask never reuses these buttons
        if pending.kind == "go":
            self.ask_text.insert("1.0", f"Run the plan shown in the log? (plan {decision_id})")
            self._button("Go", lambda: self.broker.answer(Answer.go(decision_id)))
        else:
            self.ask_text.insert("1.0", pending.text)
            if pending.kind == "yes":
                self._button("Yes", lambda: self.broker.answer(Answer.yes(decision_id)))
            else:
                for n in range(1, pending.candidates + 1):
                    self._button(str(n), self._chooser(n, decision_id))
        self._button("No", lambda: self.broker.answer(Answer.no()))
        self.ask_text["state"] = "disabled"

    def _chooser(self, n: int, decision_id: str) -> Callable[[], None]:
        return lambda: self.broker.answer(Answer.choose(n, decision_id))

    def _button(self, label: str, command: Callable[[], None]) -> None:
        # a classic button that never takes focus: a ttk button calls SetFocus on click, which
        # activates the window even with WS_EX_NOACTIVATE (measured 2026-11-04)
        tk.Button(self.ask_buttons, text=label, command=command, takefocus=0, width=6).pack(
            side="left", padx=2
        )

    def _log(self, line: str) -> None:
        self.log["state"] = "normal"
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log["state"] = "disabled"


class _NoMonitor:
    """Stands in when the deps start no monitor (pure tests): never kills, always healthy."""

    def kill_requested(self) -> bool:
        return False

    def healthy(self) -> bool:
        return True

    def last_user_input(self) -> float:
        return 0.0

    def counts(self) -> dict[str, int]:
        return {}

    def mark_ask_open(self) -> None:
        return None

    def mark_ask_closed(self) -> None:
        return None

    def maintain(self) -> None:
        return None

    def detail(self) -> str:
        return "monitor_lost"

    def close(self) -> None:
        return None
