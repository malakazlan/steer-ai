"""The live probes' way to run `steerai.probes.task_suite` in a child process: its argument
list, its rows, and the per-task seconds in them. Pure; the spawn is in the probes."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

IN_PROCESS = "in_process"
CHUNKED = "chunked"


def suite_argv(
    *, set_name: str, tasks: Sequence[str], out: Path, mode: str, floor_mb: int
) -> list[str]:
    """`python -m steerai.probes.task_suite` for `tasks` of `set_name`, one run each, in
    `mode` (`in_process` or `chunked`, the suite's default)."""
    if mode not in (IN_PROCESS, CHUNKED):
        raise ValueError(f"unknown mode {mode!r}")
    argv = [
        sys.executable,
        "-m",
        "steerai.probes.task_suite",
        "--set",
        set_name,
        "--tasks",
        ",".join(tasks),
        "--runs",
        "1",
        "--min-free-mb",
        str(floor_mb),
        "--out",
        str(out),
    ]
    return [*argv, "--in-process"] if mode == IN_PROCESS else argv


def read_rows(path: Path) -> list[dict[str, Any]]:  # Any: the suite's JSON rows
    """The suite's rows; a missing file is no rows, a line that is not a JSON object is skipped."""
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def task_rows(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The first row per task id that carries a `task`; the header and refusal rows have none."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        task = row.get("task")
        if isinstance(task, str) and task not in out:
            out[task] = row
    return out


def seconds(row: dict[str, Any]) -> float | None:
    """The row's wall seconds, None when it has none."""
    value = row.get("seconds")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
