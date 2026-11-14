"""[V2.19] item 3, [V2.16]: is the chunked suite comparable to the in-process one, and what does
chunking cost?

    uv run python -m steerai.probes.comparability --tasks g21,g22,g23

Runs the same gallery tasks once with `--in-process` and once chunked (the suite's default,
one task per child), one after the other. Prints one row per task (the two outcome tokens,
whether they are equal, the two wall seconds in the suite's rows and their difference) and a
`comparability` row: the tasks equal, and the per-task overhead of chunking from the two whole
run wall times (which include each child's process and browser start the rows' seconds leave
out). Closed values only.

Exit 75 when a pre-flight refused, else 0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from steerai.probes.memory_guard import EXIT_LOW_MEMORY, FLOOR_MB, LowMemory, preflight
from steerai.probes.suite_runner import (
    CHUNKED,
    IN_PROCESS,
    read_rows,
    seconds,
    suite_argv,
    task_rows,
)

DEFAULT_TASKS = "g21,g22,g23"


def compare(
    in_process: Sequence[dict[str, Any]],  # Any: the suite's JSON rows
    chunked: Sequence[dict[str, Any]],
    *,
    in_process_wall_s: float,
    chunked_wall_s: float,
) -> list[dict[str, Any]]:
    """Per-task rows then the summary. `equal` is None when either mode has no outcome for the
    task (a refused or aborted task is not a match, and not a mismatch)."""
    a, b = task_rows(in_process), task_rows(chunked)
    out: list[dict[str, Any]] = []
    for task in sorted(a.keys() | b.keys()):
        ra, rb = a.get(task, {}), b.get(task, {})
        oa, ob = ra.get("outcome"), rb.get("outcome")
        sa, sb = seconds(ra), seconds(rb)
        out.append(
            {
                "task": task,
                "in_process": oa,
                "chunked": ob,
                "equal": None if oa is None or ob is None else oa == ob,
                "seconds_in_process": sa,
                "seconds_chunked": sb,
                "seconds_diff": None if sa is None or sb is None else round(sb - sa, 1),
            }
        )
    n = len(out)
    out.append(
        {
            "report": "comparability",
            "tasks": n,
            "equal": sum(1 for r in out if r.get("equal") is True),
            "different": sum(1 for r in out if r.get("equal") is False),
            "unknown": sum(1 for r in out if r.get("equal") is None),
            "wall_in_process_s": round(in_process_wall_s, 1),
            "wall_chunked_s": round(chunked_wall_s, 1),
            "overhead_per_task_s": (
                round((chunked_wall_s - in_process_wall_s) / n, 1) if n else None
            ),
        }
    )
    return out


def _timed(argv: list[str]) -> tuple[int, float]:  # pragma: no cover - live
    started = time.monotonic()
    code = subprocess.run(argv, stdout=subprocess.DEVNULL, check=False).returncode  # noqa: S603 - our own module, a fixed list
    return code, time.monotonic() - started


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", default=DEFAULT_TASKS, help="gallery task ids, comma-separated")
    parser.add_argument("--min-free-mb", type=int, default=FLOOR_MB)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    tasks = [t for t in args.tasks.split(",") if t]
    walls: dict[str, float] = {}
    rows: dict[str, list[dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory(prefix="steer-comparability-") as tmp:
        for mode in (IN_PROCESS, CHUNKED):
            try:
                preflight(floor_mb=args.min_free_mb)
            except LowMemory as refused:
                sys.stderr.write(f"{refused}\n")
                return EXIT_LOW_MEMORY
            out = Path(tmp) / f"{mode}.jsonl"
            code, walls[mode] = _timed(
                suite_argv(
                    set_name="gallery", tasks=tasks, out=out, mode=mode, floor_mb=args.min_free_mb
                )
            )
            if code == EXIT_LOW_MEMORY:
                return EXIT_LOW_MEMORY
            rows[mode] = read_rows(out)
    for row in compare(
        rows[IN_PROCESS],
        rows[CHUNKED],
        in_process_wall_s=walls[IN_PROCESS],
        chunked_wall_s=walls[CHUNKED],
    ):
        sys.stdout.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
