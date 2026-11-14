"""[V2.14] 4 on the measurement probes that launch Chrome (`effect_pages`, `uia_props`): every
launch goes through the pre-flight floor and the one-browser slot, and a refusal is a clear row
and a distinct exit code. Fakes only: no Chrome."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from steerai.probes import effect_pages, uia_props
from steerai.probes.memory_guard import (
    EXIT_LOW_MEMORY,
    BrowserBusyError,
    LowMemory,
    guarded_launch,
    guarded_stop,
    one_browser,
)

PROBES = [effect_pages, uia_props]


def test_guarded_launch_refuses_below_the_floor_before_launching() -> None:
    launched: list[object] = []
    with pytest.raises(LowMemory):
        guarded_launch(launched.append, "x", floor_mb=4096, read=lambda: 100)
    assert launched == []
    with one_browser():
        pass  # the slot was never taken


def test_guarded_launch_holds_the_slot_until_guarded_stop() -> None:
    proc = guarded_launch(lambda: "proc", floor_mb=10, read=lambda: 100)
    with pytest.raises(BrowserBusyError):
        guarded_launch(lambda: "second", floor_mb=10, read=lambda: 100)
    stopped: list[tuple[str, Path]] = []
    guarded_stop(lambda p, profile: stopped.append((p, profile)), proc, Path("profile"))
    assert stopped == [("proc", Path("profile"))]
    with one_browser():
        pass  # freed by the stop


def test_a_failed_launch_frees_the_slot() -> None:
    def fails() -> str:
        raise OSError("chrome missing")

    with pytest.raises(OSError, match="chrome missing"):
        guarded_launch(fails, floor_mb=10, read=lambda: 100)
    with one_browser():
        pass


def test_a_stop_that_raises_still_frees_the_slot() -> None:
    proc = guarded_launch(lambda: "proc", floor_mb=10, read=lambda: 100)

    def broken(p: str, profile: Path) -> None:
        raise OSError("taskkill failed")

    with pytest.raises(OSError, match="taskkill"):
        guarded_stop(broken, proc, Path("profile"))
    with one_browser():
        pass


@pytest.mark.parametrize("probe", PROBES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_each_probes_launch_is_guarded(probe: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A floor no machine meets: refused (or unreadable off Windows), never launched."""
    launched: list[object] = []
    monkeypatch.setattr(probe, "launch_chrome", lambda *a, **k: launched.append((a, k)))
    monkeypatch.setitem(probe._RUN, "floor_mb", 10**12)
    with pytest.raises(LowMemory):
        probe._launch("chrome.exe", Path("profile"), 9, "http://127.0.0.1:1/")
    assert launched == []


@pytest.mark.parametrize(
    ("probe", "body", "argv"),
    [
        (effect_pages, "_sections", ["--section", "document"]),
        (uia_props, "_probe", ["--sites"]),
    ],
    ids=["effect_pages", "uia_props"],
)
def test_a_refusal_is_a_row_a_message_and_the_low_memory_exit(
    probe: Any,
    body: str,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refused(args: Any) -> int:  # Any: the parsed arguments
        raise LowMemory(100, args.min_free_mb)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(probe, body, refused)
    monkeypatch.setitem(probe._RUN, "floor_mb", probe._RUN["floor_mb"])  # restored after
    assert probe.main([*argv, "--min-free-mb", "5000"]) == EXIT_LOW_MEMORY
    captured = capsys.readouterr()
    row = json.loads(captured.out.strip().splitlines()[-1])
    assert row["refused"] == "low_memory"
    assert (row["free_mb"], row["floor_mb"]) == (100, 5000)
    assert "below the floor 5000 MB" in captured.err
    assert probe._RUN["floor_mb"] == 5000
