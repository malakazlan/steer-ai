"""The live suite in chunks ([V2.14] decision 4): each chunk of tasks runs in its own child
process, one at a time, after a pre-flight free-memory check; the child's wall time is capped per
task; on a cap, an interrupt or a refusal only that child's process tree and its own temp folder
are removed, never another process.

The child is `python -m steerai.probes.task_suite` with `--tasks`, `--run-number`, `--out` and
`--tmp-root` set by this runner; its rows are read back from its own out file and passed on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from steerai.probes.memory_guard import (
    EXIT_LOW_MEMORY,
    LowMemory,
    free_physical_mb,
    preflight,
    refusal_row,
)

REMOVE_TRIES = 10
REMOVE_WAIT_S = 0.5
"""A killed Chrome may hold its profile's files for a moment (stop_chrome retries the same way)."""


@dataclass(frozen=True, slots=True)
class Unit:
    task_id: str
    number: int
    """The run number (`--runs`)."""


class Child(Protocol):
    pid: int

    def wait(self, timeout: float | None = None) -> int: ...


def plan_chunks(units: Sequence[Unit], chunk_size: int) -> list[list[Unit]]:
    """Consecutive units in chunks of at most `chunk_size`; a chunk never spans two run numbers
    (the child runs one number)."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    chunks: list[list[Unit]] = []
    for unit in units:
        last = chunks[-1] if chunks else None
        if last is None or len(last) >= chunk_size or last[0].number != unit.number:
            chunks.append([unit])
        else:
            last.append(unit)
    return chunks


def _start(argv: list[str]) -> Child:
    return subprocess.Popen(argv)  # noqa: S603 - our own module, a fixed argument list, no shell


def kill_tree(pid: int) -> None:
    """End one child and every process it started (its Chrome), by that child's pid only."""
    subprocess.run(  # noqa: S603 - taskkill with our child's pid, no shell
        ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607 - taskkill on PATH
        capture_output=True,
        check=False,
    )


def remove_tree(
    path: Path, *, sleep: Callable[[float], None] = time.sleep, tries: int = REMOVE_TRIES
) -> None:
    """Remove one of this runner's own folders, retried while a killed Chrome lets go."""
    for _ in range(tries):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError:
            sleep(REMOVE_WAIT_S)
        else:
            return


def _rows(out: Path) -> list[dict[str, Any]]:  # Any: the suite's JSON rows
    if not out.is_file():
        return []
    rows = []
    for line in out.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict) and "task" in row:
                rows.append(row)
    return rows


def run_chunked(  # noqa: PLR0913 - the plan, the child's arguments and the injected OS calls
    chunks: Sequence[Sequence[Unit]],
    *,
    base_args: Sequence[str],
    cap_s: float,
    floor_mb: int,
    tmp_parent: Path,
    write_row: Callable[[dict[str, Any]], None],  # Any: the suite's JSON rows
    read_free: Callable[[], int] | None = None,
    start: Callable[[list[str]], Child] | None = None,
    kill_tree: Callable[[int], None] = kill_tree,
    remove: Callable[[Path], None] = remove_tree,
) -> int:
    """Run every chunk, one child at a time. Returns 0, or EXIT_LOW_MEMORY when a pre-flight
    (here or in a child) refused; a KeyboardInterrupt is raised after the cleanup. `read_free`
    and `start` default to this module's own at call time, so a test can replace them."""
    read_free = free_physical_mb if read_free is None else read_free
    start = _start if start is None else start
    for index, chunk in enumerate(chunks):
        try:
            preflight(floor_mb=floor_mb, read=read_free)
        except LowMemory as refused:
            remaining = [f"{u.task_id}:{u.number}" for c in chunks[index:] for u in c]
            write_row(refusal_row(refused, remaining))
            sys.stderr.write(f"{refused}\n")
            return EXIT_LOW_MEMORY
        code = _one_chunk(
            index,
            chunk,
            base_args=base_args,
            cap_s=cap_s,
            floor_mb=floor_mb,
            tmp_parent=tmp_parent,
            write_row=write_row,
            start=start,
            kill=kill_tree,
            remove=remove,
        )
        if code == EXIT_LOW_MEMORY:
            return EXIT_LOW_MEMORY
    return 0


def _one_chunk(  # noqa: PLR0913 - one chunk of run_chunked, its arguments passed through
    index: int,
    chunk: Sequence[Unit],
    *,
    base_args: Sequence[str],
    cap_s: float,
    floor_mb: int,
    tmp_parent: Path,
    write_row: Callable[[dict[str, Any]], None],  # Any: the suite's JSON rows
    start: Callable[[list[str]], Child],
    kill: Callable[[int], None],
    remove: Callable[[Path], None],
) -> int | None:
    root = tmp_parent / f"chunk-{index + 1}"
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    out = root / "rows.jsonl"
    argv = [
        sys.executable,
        "-m",
        "steerai.probes.task_suite",
        *base_args,
        "--tasks",
        ",".join(u.task_id for u in chunk),
        "--run-number",
        str(chunk[0].number),
        "--out",
        str(out),
        "--tmp-root",
        str(root / "tmp"),
        "--min-free-mb",
        str(floor_mb),
    ]
    child = start(argv)
    aborted: str | None = None
    code: int | None = None
    try:
        code = child.wait(timeout=cap_s * len(chunk))
    except subprocess.TimeoutExpired:
        aborted = "task_cap"
    except KeyboardInterrupt:
        aborted = "interrupted"
        raise
    finally:
        if aborted is not None:
            kill(child.pid)
            child.wait()
        _pass_rows(chunk, _rows(out), aborted, write_row)
        remove(root)
    return code


def _pass_rows(
    chunk: Sequence[Unit],
    rows: list[dict[str, Any]],  # Any: the suite's JSON rows
    aborted: str | None,
    write_row: Callable[[dict[str, Any]], None],  # Any: the suite's JSON rows
) -> None:
    done = set()
    for row in rows:
        done.add((str(row.get("task")), int(row.get("run") or 0)))
        write_row(row)
    if aborted is None:
        return
    for unit in chunk:
        if (unit.task_id, unit.number) not in done:
            write_row({"task": unit.task_id, "run": unit.number, "aborted": aborted})
