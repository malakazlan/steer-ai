"""The overlay window (packet launcher-2026-11-04). Tk needs a desktop, so these are `windows`
tests with a hidden root; the foreground postcondition test clicks a real button with a tagged
`SendInput` while another window holds foreground (schema gate item 6)."""

from __future__ import annotations

import contextlib
import io
import queue
import threading
import time
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from steerai.app.overlay import (
    WS_EX_NOACTIVATE,
    WS_EX_TOPMOST,
    ClosableMonitor,
    Overlay,
    QueueWriter,
    RunnerThread,
    task_ex_style,
    toplevel_hwnd,
)
from steerai.orchestrator.gate import Ask
from steerai.probes.windows import WindowInfo
from steerai.run import Deps

pytestmark = pytest.mark.windows

CHROME = WindowInfo(hwnd=0x10, title="Amir Khan - Chat - Google Chrome", pid=500)
CONSOLE = WindowInfo(hwnd=0x30, title="python -m steerai.app", pid=700)


def _ask(decision_id: str = "d-1") -> Ask:
    return Ask(
        decision_id=decision_id,
        task_id="t",
        action_key="k",
        verb="send",
        target_role="Button",
        risk="R2",
        expect={},
        params={"text": "hello"},
        target_name="Send",
    )


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    try:
        window = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - no desktop
        pytest.skip(f"no Tk display: {exc}")
    window.withdraw()
    yield window
    with contextlib.suppress(tk.TclError):
        window.destroy()


def _deps(**overrides: Any) -> Deps:
    return Deps(
        list_windows=lambda: [CHROME, CONSOLE],
        exclusions=lambda: ({CONSOLE.hwnd}, {CONSOLE.pid}),
        acquire_foreground=lambda _h: "direct",
        open_ledger=lambda _r, _t: None,  # type: ignore[arg-type, return-value]
        build_session=lambda _b: None,  # type: ignore[arg-type, return-value]
        **overrides,
    )


def _overlay(root: tk.Tk, tmp_path: Path, runner: Any) -> Overlay:
    return Overlay(
        root=root,
        deps=_deps(),
        list_windows=lambda: [CHROME, CONSOLE],
        exclusions=lambda: ({CONSOLE.hwnd}, {CONSOLE.pid}),
        ledger_dir=tmp_path / "l",
        trace_dir=tmp_path / "t",
        runner=runner,
    )


def _never_called(*_args: Any, **_kwargs: Any) -> int:
    raise AssertionError("the runner must not start")


def test_queue_writer_delivers_whole_lines() -> None:
    lines: queue.Queue[str] = queue.Queue()
    writer = QueueWriter(lines)
    writer.write("ASK d-1 send")
    writer.write(" R2 Button\nnext")
    assert lines.get_nowait() == "ASK d-1 send R2 Button"
    assert lines.empty()
    writer.flush()
    assert lines.get_nowait() == "next"
    assert writer.isatty() is False


def test_the_picker_excludes_steers_own_tree_and_preselects_nothing(
    root: tk.Tk, tmp_path: Path
) -> None:
    overlay = _overlay(root, tmp_path, runner=_never_called)
    assert list(overlay.window_box["values"]) == [f"{CHROME.title}  [0x10]"]
    assert overlay.selected_window() is None


def test_run_needs_a_goal_and_a_window_and_refuses_a_second_task(
    root: tk.Tk, tmp_path: Path
) -> None:
    started = threading.Event()
    release = threading.Event()

    def runner(options: Any, deps: Any, *, stdin: Any, stdout: Any, stderr: Any) -> int:
        started.set()
        release.wait(5.0)
        return 0

    overlay = _overlay(root, tmp_path, runner)
    overlay.run_clicked()
    assert "a goal and a target window are needed" in overlay.log.get("1.0", "end")
    overlay.goal.insert(0, "send hello there to Amir")
    overlay.window_box.current(0)
    overlay.run_clicked()
    assert started.wait(2.0)
    assert overlay.running() is True
    overlay.run_clicked()
    assert "one at a time" in overlay.log.get("1.0", "end")
    release.set()
    for _ in range(100):
        overlay.drain()
        if not overlay.running():
            break
        time.sleep(0.02)
    assert overlay.running() is False
    assert overlay.status["text"] == "exit 0"


def test_an_ask_shows_buttons_that_post_typed_answers(root: tk.Tk, tmp_path: Path) -> None:
    tokens: list[str] = []

    def runner(options: Any, deps: Any, *, stdin: Any, stdout: Any, stderr: Any) -> int:
        confirmer = deps.make_confirmer(stdin, stderr, None)
        tokens.append(confirmer.ask_go("abc123def456"))
        tokens.append(confirmer.ask(_ask()))
        return 0

    overlay = _overlay(root, tmp_path, runner)
    overlay.goal.insert(0, "send hello there to Amir")
    overlay.window_box.current(0)
    overlay.run_clicked()
    assert _wait_for(lambda: overlay.broker.pending() is not None)
    overlay.drain()
    labels = [b["text"] for b in overlay.ask_buttons.winfo_children()]
    assert labels == ["Go", "No"]
    assert "yes:" not in overlay.ask_text.get("1.0", "end")
    _first_button(overlay).invoke()
    assert _wait_for(lambda: len(tokens) == 1)
    assert tokens == ["go"]
    assert _wait_for(lambda: overlay.broker.pending() is not None)
    overlay.drain()
    labels = [b["text"] for b in overlay.ask_buttons.winfo_children()]
    assert labels == ["Yes", "No"]
    _first_button(overlay).invoke()
    assert _wait_for(lambda: len(tokens) == 2)
    assert tokens[1] == "yes:d-1"


def test_closing_the_window_is_the_kill(root: tk.Tk, tmp_path: Path) -> None:
    tokens: list[str] = []

    def runner(options: Any, deps: Any, *, stdin: Any, stdout: Any, stderr: Any) -> int:
        monitor = deps.start_monitor(options)
        confirmer = deps.make_confirmer(stdin, stderr, monitor)
        tokens.append(confirmer.ask(_ask()))
        return 3

    overlay = _overlay(root, tmp_path, runner)
    overlay.start(overlay.options_for("send hello there to Amir", CHROME.hwnd))
    assert _wait_for(lambda: overlay.broker.pending() is not None)
    overlay.close()
    assert tokens == ["killed"]
    assert overlay.closed.is_set()


def test_a_closable_monitor_ors_the_ui_flag_into_the_kill() -> None:
    class Inner:
        def kill_requested(self) -> bool:
            return False

        def healthy(self) -> bool:
            return True

    closed = threading.Event()
    monitor = ClosableMonitor(Inner(), closed)
    assert monitor.kill_requested() is False
    assert monitor.healthy() is True
    closed.set()
    assert monitor.kill_requested() is True


def test_task_mode_sets_no_activate_and_topmost_and_a_tagged_click_keeps_foreground(
    root: tk.Tk, tmp_path: Path
) -> None:
    """Schema gate item 6: with another window foreground, a tagged SendInput click on Yes
    leaves the foreground unchanged and the token reaches the broker; latency measured."""
    import ctypes  # noqa: PLC0415

    from steerai.probes.win_input import click_at, foreground_hwnd  # noqa: PLC0415

    tokens: list[str] = []
    stamps: list[float] = []

    def runner(options: Any, deps: Any, *, stdin: Any, stdout: Any, stderr: Any) -> int:
        confirmer = deps.make_confirmer(stdin, stderr, None)
        tokens.append(confirmer.ask(_ask()))
        stamps.append(time.perf_counter())
        return 0

    other = tk.Toplevel(root)
    other.geometry("300x200+50+50")
    other.update()
    root.deiconify()
    root.geometry("560x520+400+50")
    root.update()
    overlay = _overlay(root, tmp_path, runner)
    overlay.start(overlay.options_for("send hello there to Amir", CHROME.hwnd))
    hwnd = toplevel_hwnd(root)
    style = task_ex_style(hwnd, task=True)
    assert style & WS_EX_NOACTIVATE
    assert style & WS_EX_TOPMOST
    assert _wait_for(lambda: overlay.broker.pending() is not None)
    overlay.drain()
    root.update()
    other_hwnd = toplevel_hwnd(other)
    ctypes.windll.user32.SetForegroundWindow(other_hwnd)
    time.sleep(0.2)
    before = foreground_hwnd()
    if before != other_hwnd:
        pytest.skip("could not make the other window foreground in this session")
    yes = overlay.ask_buttons.winfo_children()[0]
    x = yes.winfo_rootx() + yes.winfo_width() // 2
    y = yes.winfo_rooty() + yes.winfo_height() // 2
    clicked = time.perf_counter()
    click_at(x, y)
    for _ in range(100):
        root.update()
        if tokens:
            break
        time.sleep(0.02)
    assert tokens == ["yes:d-1"]
    assert foreground_hwnd() == before, "the click never took foreground"
    latency_ms = (stamps[0] - clicked) * 1000
    assert latency_ms < 2000
    print(f"click-to-token latency {latency_ms:.0f} ms")  # noqa: T201 - measured, for the PR


def _first_button(overlay: Overlay) -> tk.Button:
    button = overlay.ask_buttons.winfo_children()[0]
    assert isinstance(button, tk.Button)
    return button


def _wait_for(condition: Any, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_runner_thread_is_one_thread_for_all_jobs() -> None:
    worker = RunnerThread()
    seen: list[str] = []
    worker.submit(lambda: seen.append(threading.current_thread().name))
    worker.submit(lambda: seen.append(threading.current_thread().name))
    assert worker.wait_idle(2.0)
    assert seen == ["steer-runner", "steer-runner"]
    assert io is not None
