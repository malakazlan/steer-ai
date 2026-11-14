"""[V2.19] item 3: the Chrome working set and task times, to set FLOOR_MB and the task cap from
data ([V2.14] 4).

    uv run python -m steerai.probes.working_set --tasks g21,g22,g23

Runs those gallery tasks through `steerai.probes.task_suite --in-process` in one child process
and, every 0.5 s until it exits, reads free physical memory and the summed working set and peak
working set of the child's whole process tree (Chrome included). Prints one row per task (its
wall seconds from the suite's own row) and one `working_set` summary row: peak tree MB, minimum
free MB. Closed values and counts only; no page text. The memory facts: `process_tree`.

Exit 75 when the pre-flight refuses, else 0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from steerai.probes.memory_guard import (
    EXIT_LOW_MEMORY,
    FLOOR_MB,
    LowMemory,
    free_physical_mb,
    preflight,
)
from steerai.probes.process_tree import Sample, read_tree, summarize
from steerai.probes.suite_runner import IN_PROCESS, read_rows, seconds, suite_argv, task_rows

SAMPLE_EVERY_S = 0.5
DEFAULT_TASKS = "g21,g22,g23"


def sample_until_exit(  # noqa: PLR0913 - the injected clock, poll, readers and wait
    poll: Callable[[], int | None],
    *,
    read_free: Callable[[], int],
    read_proc: Callable[[], tuple[float, float, int]],
    clock: Callable[[], float],
    wait: Callable[[float], None],
    every_s: float = SAMPLE_EVERY_S,
) -> list[Sample]:
    """A sample before each check of `poll`, until it returns an exit code. A reading that
    raises OSError is skipped, never guessed; the first sample is taken even if the child
    already exited."""
    samples: list[Sample] = []
    started = clock()
    while True:
        try:
            free = read_free()
            total, peak, count = read_proc()
            samples.append(Sample(round(clock() - started, 2), free, total, peak, count))
        except OSError:
            pass
        if poll() is not None:
            return samples
        wait(every_s)


def report(
    samples: Sequence[Sample], rows: Sequence[dict[str, Any]], *, exit_code: int
) -> list[dict[str, Any]]:  # Any: JSON values
    """One row per task (id, wall seconds, outcome token) then the summary row."""
    out: list[dict[str, Any]] = [
        {"task": task, "seconds": seconds(row), "outcome": row.get("outcome")}
        for task, row in task_rows(rows).items()
    ]
    out.append({"report": "working_set", "suite_exit": exit_code, **summarize(samples)})
    return out


def _run(argv: list[str]) -> tuple[int, list[Sample]]:  # pragma: no cover - live
    child = subprocess.Popen(argv)  # noqa: S603 - our own module, a fixed list
    try:
        samples = sample_until_exit(
            child.poll,
            read_free=free_physical_mb,
            read_proc=lambda: read_tree(child.pid),
            clock=time.monotonic,
            wait=time.sleep,
        )
    except BaseException:
        from steerai.probes.suite_chunks import kill_tree  # noqa: PLC0415

        kill_tree(child.pid)  # our own child, started here
        raise
    return int(child.returncode), samples


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", default=DEFAULT_TASKS, help="gallery task ids, comma-separated")
    parser.add_argument("--min-free-mb", type=int, default=FLOOR_MB)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    try:
        preflight(floor_mb=args.min_free_mb)
    except LowMemory as refused:
        sys.stderr.write(f"{refused}\n")
        return EXIT_LOW_MEMORY
    with tempfile.TemporaryDirectory(prefix="steer-workingset-") as tmp:
        out = Path(tmp) / "rows.jsonl"
        code, samples = _run(
            suite_argv(
                set_name="gallery",
                tasks=[t for t in args.tasks.split(",") if t],
                out=out,
                mode=IN_PROCESS,
                floor_mb=args.min_free_mb,
            )
        )
        for row in report(samples, read_rows(out), exit_code=code):
            sys.stdout.write(json.dumps(row) + "\n")
    return EXIT_LOW_MEMORY if code == EXIT_LOW_MEMORY else 0


if __name__ == "__main__":
    raise SystemExit(main())
