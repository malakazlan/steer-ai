"""Phase 4 bar probe (section 12: "repeat tasks with zero LLM calls"; 5.12 [v2.33], [v2.34]): the
request run once to fill the plan cache, then repeated; each repeat must end `done` with no
planner call, and from the trusted state on with no model call at all.

Each run opens a fresh Notepad, runs `steerai.run` in process with `--goal` bound to that window
(a person who answers `go` and declines every other ask), reads the trace for the cache event,
the status and the model calls, then closes Notepad. The cache lives in a throwaway folder, never
the person's own. Prints tokens and counts only.

Usage (Windows, the person away: the idle gate waits before every run; calls the planner):
    uv run python -m steerai.probes.phase4_bar --repeats 5
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steerai import run as run_mod
from steerai.memory.plan_cache import PlanCache
from steerai.probes.windows import session_locked, wait_window_for_pid

GOAL = "type hello from steer in notepad"
IDLE_S = 60.0
TRUSTED_FROM = 4
"""The run number from which the cached plan is trusted: three verified runs before it."""


class _LastInput(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


def wait_until_idle(limit_s: float = 1800.0) -> None:
    """The person away: unlocked and no input for a minute, before anything is typed."""
    user32: Any = ctypes.WinDLL("user32")  # Any: a ctypes library handle
    kernel32: Any = ctypes.WinDLL("kernel32")  # Any: a ctypes library handle
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        info = _LastInput()
        info.cbSize = ctypes.sizeof(info)
        user32.GetLastInputInfo(ctypes.byref(info))
        idle = ((int(kernel32.GetTickCount()) & 0xFFFFFFFF) - info.dwTime) % (1 << 32) / 1000.0
        if session_locked() is False and idle >= IDLE_S:
            return
        time.sleep(5.0)
    raise TimeoutError("the person stayed active")


NOTEPAD = Path(r"C:\Windows\System32\notepad.exe")


@dataclass
class GoOnly:
    """The person: approves the plan, declines anything the gate asks (no ask is expected)."""

    asks: int = 0

    def ask(self, ask: Any) -> str:  # Any: steerai.orchestrator.gate.Ask
        self.asks += 1
        return f"no:{ask.decision_id}"

    def ask_go(self, go_id: str) -> str:  # noqa: ARG002 - the protocol's shape
        return "go"


def one_run(cache: PlanCache, number: int, root: Path) -> dict[str, Any]:
    wait_until_idle()
    proc = subprocess.Popen([str(NOTEPAD)])  # noqa: S603 - a fixed system path
    work = root / f"run{number}"
    try:
        window = wait_window_for_pid(proc.pid, image="notepad.exe", process=proc)
        person = GoOnly()
        options = run_mod.Options(
            plan=work / "task.plan.json",
            window=None,
            hwnd=window.hwnd,
            task_id=f"p4-{number}",
            ledger_dir=work / "ledger",
            trace=work / "trace.jsonl",
            max_risk="R3",
            max_steps=12,
            verify_deadline_s=3.0,
            warmup_s=3.0,
            allow_plan_change=False,
            allow_window_change=False,
            goal=GOAL,
        )
        deps = run_mod._real_deps().replace(
            make_confirmer=lambda *_: person, open_windows=lambda: [], plan_cache=cache
        )
        stdout = io.StringIO()
        started = time.monotonic()
        run_mod.run(options, deps, stdin=io.StringIO(""), stdout=stdout, stderr=io.StringIO())
        elapsed = time.monotonic() - started
        records = [json.loads(x) for x in (work / "trace.jsonl").read_text("utf-8").splitlines()]
        payload = json.loads(stdout.getvalue())
        cache_events = [r.get("event") for r in records if r.get("kind") == "cache"]
        calls: dict[str, Any] = next((r for r in records if r.get("kind") == "model_calls"), {})
        judged_by = [r.get("model") for r in records if r.get("kind") == "goal_match"]
        return {
            "run": number,
            "status": payload.get("status"),
            "detail": payload.get("detail"),
            "cache": cache_events,
            "planner_calls": calls.get("planner", 0),
            "goal_match_calls": calls.get("goal_match", 0),
            "judged_by": judged_by,
            "asks": person.asks,
            "seconds": round(elapsed, 1),
        }
    finally:
        subprocess.run(  # noqa: S603 - fixed argv
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],  # noqa: S607 - taskkill on PATH
            check=False,
            capture_output=True,
        )


def verdict(rows: Sequence[dict[str, Any]]) -> tuple[str, bool]:
    """The bar: the fill run and every repeat end `done`, no repeat calls the planner, and every
    repeat from the trusted state on (the fourth verified run) calls no model at all."""
    repeats = rows[1:]
    done = sum(r["status"] == "done" for r in repeats)
    unplanned = sum(r["planner_calls"] == 0 for r in repeats)
    silent = [r for r in repeats if r["planner_calls"] == 0 and r["goal_match_calls"] == 0]
    trusted = repeats[TRUSTED_FROM - 2 :]
    passed = (
        bool(rows)
        and rows[0]["status"] == "done"
        and done == len(repeats)
        and unplanned == len(repeats)
        and all(r["goal_match_calls"] == 0 for r in trusted)
    )
    line = (
        f"repeats {len(repeats)}: done {done}, planner calls 0 in {unplanned}, "
        f"all model calls 0 in {len(silent)}, bar {'met' if passed else 'missed'}"
    )
    return line, passed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4 bar: repeats with zero model calls.")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    from steerai.platform import dpapi  # noqa: PLC0415 - Windows only

    root = Path(tempfile.mkdtemp(prefix="steer-p4-"))
    cache = PlanCache(root / "cache", protect=dpapi.protect, unprotect=dpapi.unprotect)
    rows = []
    try:
        for number in range(1, args.repeats + 2):
            row = one_run(cache, number, root)
            rows.append(row)
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    line, passed = verdict(rows)
    sys.stdout.write(line + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
