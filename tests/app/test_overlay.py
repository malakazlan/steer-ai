"""The overlay window (packet launcher-2026-11-04). Tk needs a desktop, so these are `windows`
tests with a hidden root; the runner is the fake child `tests/app/fake_runner.py`; the
foreground postcondition test clicks a real button with a tagged `SendInput` while another
window holds foreground (schema gate item 6)."""

from __future__ import annotations

import contextlib
import sys
import time
import tkinter as tk
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from steerai.app.overlay import (
    DESKTOP_HWND,
    DESKTOP_LABEL,
    WS_EX_NOACTIVATE,
    WS_EX_TOPMOST,
    Overlay,
    task_ex_style,
    toplevel_hwnd,
)
from steerai.app.runner_process import RunnerProcess
from steerai.probes.windows import WindowInfo

pytestmark = pytest.mark.windows

CHROME = WindowInfo(hwnd=0x10, title="Amir Khan - Chat - Google Chrome", pid=500)
CONSOLE = WindowInfo(hwnd=0x30, title="python -m steerai.app", pid=700)
FAKE = Path(__file__).with_name("fake_runner.py")


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


def _fake_child(script: str) -> Any:
    def make(argv: Sequence[str]) -> RunnerProcess:
        assert "--goal" in argv, "the runner's own options are passed through"
        return RunnerProcess([sys.executable, str(FAKE), script])

    return make


def _overlay(root: tk.Tk, tmp_path: Path, script: str = "go-then-ask") -> Overlay:
    return Overlay(
        root=root,
        list_windows=lambda: [CHROME, CONSOLE],
        exclusions=lambda: ({CONSOLE.hwnd}, {CONSOLE.pid}),
        ledger_dir=tmp_path / "l",
        trace_dir=tmp_path / "t",
        make_process=_fake_child(script),
    )


def _wait_for(condition: Any, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def _buttons(overlay: Overlay) -> list[tk.Button]:
    out: list[tk.Button] = []
    for child in overlay.ask_buttons.winfo_children():
        assert isinstance(child, tk.Button)
        out.append(child)
    return out


def _pump_until(overlay: Overlay, condition: Any, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        overlay.drain()
        overlay.root.update()
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_the_picker_excludes_steers_own_tree_and_preselects_nothing(
    root: tk.Tk, tmp_path: Path
) -> None:
    overlay = _overlay(root, tmp_path)
    assert list(overlay.window_box["values"]) == [DESKTOP_LABEL, f"{CHROME.title}  [0x10]"]
    assert overlay.selected_window() is None


def test_a_window_whose_cloak_cannot_be_read_is_not_offered(root: tk.Tk, tmp_path: Path) -> None:
    """Issue #48: picking it would end in `cloak_unreadable`; the person never sees a dead end."""
    unclear = WindowInfo(hwnd=0x20, title="Inbox - Google Chrome", pid=500, cloak_unreadable=True)
    overlay = Overlay(
        root=root,
        list_windows=lambda: [CHROME, unclear, CONSOLE],
        exclusions=lambda: ({CONSOLE.hwnd}, {CONSOLE.pid}),
        ledger_dir=tmp_path / "l",
        trace_dir=tmp_path / "t",
        make_process=_fake_child("go-then-ask"),
    )
    assert list(overlay.window_box["values"]) == [DESKTOP_LABEL, f"{CHROME.title}  [0x10]"]


def test_the_desktop_choice_runs_the_task_with_desktop(root: tk.Tk, tmp_path: Path) -> None:
    overlay = _overlay(root, tmp_path)
    argv = overlay.argv_for("open notepad", DESKTOP_HWND)
    assert "--desktop" in argv
    assert "--hwnd" not in argv


def test_run_needs_a_goal_and_a_window(root: tk.Tk, tmp_path: Path) -> None:
    overlay = _overlay(root, tmp_path)
    overlay.run_clicked()
    assert "a goal and a target window are needed" in overlay.log.get("1.0", "end")
    assert overlay.process is None


def test_a_task_runs_in_a_child_with_the_asks_as_buttons(root: tk.Tk, tmp_path: Path) -> None:
    overlay = _overlay(root, tmp_path)
    overlay.goal.insert(0, "send hello there to Amir")
    overlay.window_box.current(0)
    overlay.run_clicked()
    assert overlay.running() is True
    overlay.run_clicked()
    assert "one at a time" in overlay.log.get("1.0", "end")
    assert _pump_until(overlay, lambda: [b["text"] for b in _buttons(overlay)] == ["Go", "No"])
    assert "plan 0123456789ab" in overlay.log.get("1.0", "end")
    _buttons(overlay)[0].invoke()
    assert _pump_until(overlay, lambda: [b["text"] for b in _buttons(overlay)] == ["Yes", "No"])
    assert "text: hello there" in overlay.ask_text.get("1.0", "end")
    assert "yes:" not in overlay.ask_text.get("1.0", "end")
    _buttons(overlay)[0].invoke()
    assert _pump_until(overlay, lambda: overlay.exit_code is not None)
    assert overlay.exit_code == 0
    assert overlay.status["text"] == "done (exit 0)"
    assert overlay.running() is False
    assert _buttons(overlay) == []


def test_a_which_ask_shows_numbered_buttons(root: tk.Tk, tmp_path: Path) -> None:
    overlay = _overlay(root, tmp_path, script="which")
    overlay.start(overlay.argv_for("message Amir", CHROME.hwnd))
    assert _pump_until(overlay, lambda: len(_buttons(overlay)) == 2)
    _buttons(overlay)[0].invoke()  # Go
    assert _pump_until(overlay, lambda: [b["text"] for b in _buttons(overlay)] == ["1", "2", "No"])
    _buttons(overlay)[1].invoke()  # the second candidate
    assert _pump_until(overlay, lambda: overlay.exit_code is not None)
    assert overlay.process is not None
    assert overlay.process.result["answer"] == "2:d-2"


def test_closing_the_window_is_the_kill(root: tk.Tk, tmp_path: Path) -> None:
    overlay = _overlay(root, tmp_path)
    overlay.start(overlay.argv_for("send hello there to Amir", CHROME.hwnd))
    assert _pump_until(overlay, lambda: len(_buttons(overlay)) == 2)
    process = overlay.process
    assert process is not None
    overlay.close()
    assert _wait_for(lambda: process.exit_code is not None)
    assert process.killed is True
    assert process.exit_code != 0


def test_task_mode_keeps_foreground_on_a_tagged_click(root: tk.Tk, tmp_path: Path) -> None:
    """Schema gate item 6: with another window foreground, a tagged SendInput click on Go
    leaves the foreground unchanged and the token reaches the child; latency measured."""
    from steerai.probes.win_input import click_at, foreground_hwnd  # noqa: PLC0415

    other = tk.Toplevel(root)
    other.geometry("300x200+50+50")
    other.update()
    root.deiconify()
    root.geometry("560x520+400+50")
    root.update()
    overlay = _overlay(root, tmp_path)
    overlay.start(overlay.argv_for("send hello there to Amir", CHROME.hwnd))
    style = task_ex_style(toplevel_hwnd(root), task=True)
    assert style & WS_EX_NOACTIVATE
    assert style & WS_EX_TOPMOST
    assert _pump_until(overlay, lambda: len(_buttons(overlay)) == 2)
    root.update()
    other_hwnd = toplevel_hwnd(other)
    import ctypes  # noqa: PLC0415

    ctypes.windll.user32.SetForegroundWindow(other_hwnd)
    time.sleep(0.2)
    before = foreground_hwnd()
    if before != other_hwnd:
        pytest.skip("could not make the other window foreground in this session")
    go = _buttons(overlay)[0]
    x = go.winfo_rootx() + go.winfo_width() // 2
    y = go.winfo_rooty() + go.winfo_height() // 2
    clicked = time.perf_counter()
    click_at(x, y)
    assert _pump_until(overlay, lambda: [b["text"] for b in _buttons(overlay)] == ["Yes", "No"])
    latency_ms = (time.perf_counter() - clicked) * 1000
    assert foreground_hwnd() == before, "the click never took foreground"
    assert latency_ms < 3000
    print(f"click-to-next-ask latency {latency_ms:.0f} ms")  # noqa: T201 - measured, for the PR
    overlay.close()
