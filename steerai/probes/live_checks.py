"""[V2.19] item 3: every pending live check, in one resumable command and one report.

    uv run python -m steerai.probes.live_checks --dir <folder>

The checks run one at a time, in the order of what each one unblocks. Before each one the
runner prints what it unblocks, its expected duration and its memory need, then runs the
free-memory pre-flight. Each check's output goes to `<dir>/<name>.out` and `.err`. Its result is
one row in `<dir>/report.jsonl`, and the state in `<dir>/state.json` makes a second run skip
what already finished.

- A check that refuses for memory (exit 75) stops the run, so the person can free memory and
  start the same command again.
- A check whose probe is not built yet is reported as `not_built` and skipped. It is never
  silently missing.

The durations are estimates and the memory need is the suite's floor. Both are unmeasured until
the first check has measured them. The API key is passed to the child environment from
`steerai/.env` when it is not already set, and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steerai.probes.memory_guard import EXIT_LOW_MEMORY, FLOOR_MB, LowMemory, preflight

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
KEY_NAME = "anthropic-api-key"
KEY_VAR = "ANTHROPIC_API_KEY"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    unblocks: str
    estimate_min: int
    """Expected minutes: an estimate, unmeasured."""
    args: tuple[str, ...] | None
    """The arguments after `python -m`, with `{dir}` for the report folder; None: not built."""


CHECKS: tuple[Check, ...] = (
    Check(
        "working_set",
        "the memory floor and the task cap",
        15,
        ("steerai.probes.working_set", "--tasks", "g21,g22,g23"),
    ),
    Check(
        "shadow_gallery",
        "the 2a shadow table (3c switch-on)",
        25,
        (
            "steerai.probes.task_suite",
            "--set",
            "gallery",
            "--tasks",
            "g21,g22,g23,g25",
            "--runs",
            "1",
            "--keep",
            "{dir}/keep",
            "--out",
            "{dir}/shadow_gallery.jsonl",
        ),
    ),
    Check(
        "shadow_dev",
        "the 2a shadow table (3c switch-on)",
        40,
        (
            "steerai.probes.task_suite",
            "--set",
            "dev",
            "--runs",
            "1",
            "--keep",
            "{dir}/keep",
            "--out",
            "{dir}/shadow_dev.jsonl",
        ),
    ),
    Check("landmarks", "the query classifier's landmark fact (3c switch-on)", 10, None),
    Check(
        "instant_search_controls",
        "the 3d fixture (3c switch-on)",
        5,
        ("pytest", "tests/probes/test_instant_search.py", "-m", "probe", "-q", "-o", "addopts="),
    ),
    Check(
        "site_samples",
        "the watch deadlines (25 real-site samples)",
        30,
        ("steerai.probes.effect_pages", "--section", "sites", "--runs", "1"),
    ),
    Check("multiline", "whether the query-field rule can recover anything", 10, None),
    Check(
        "query_fields",
        "the query-class analysis ([V2.14] 2)",
        20,
        ("steerai.probes.query_fields", "--out", "{dir}/query_fields.jsonl"),
    ),
    Check("scrolled", "the scrolled token ([V2.18])", 10, None),
    Check("controller_for", "the field-owned rule's read cost ([V2.15] Q4)", 10, None),
    Check(
        "comparability",
        "chunked against in-process ([V2.16])",
        20,
        ("steerai.probes.comparability", "--tasks", "g21,g22,g23"),
    ),
)


def plan_lines(checks: Sequence[Check], done: Mapping[str, Any], floor_mb: int) -> list[str]:
    """One line per check, as printed before the run."""
    lines = []
    for n, check in enumerate(checks, 1):
        state = "done" if check.name in done else ("not built" if check.args is None else "to run")
        lines.append(
            f"{n:2}. {check.name}: {state}; unblocks {check.unblocks}; "
            f"~{check.estimate_min} min (estimate); needs >= {floor_mb} MB free"
        )
    return lines


def child_env(env: Mapping[str, str], env_file: Path) -> dict[str, str]:
    """`env` with the API key added from `env_file` when it is not set; the key is never
    printed or logged."""
    out = dict(env)
    out.setdefault("PYTHONIOENCODING", "utf-8")
    out.pop("VIRTUAL_ENV", None)
    if out.get(KEY_VAR) or not env_file.is_file():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == KEY_NAME:
            out[KEY_VAR] = value.strip().strip("\"'")
            break
    return out


def _argv(check: Check, folder: Path) -> list[str]:
    if check.args is None:
        raise ValueError(f"{check.name} is not built")
    return [sys.executable, "-m", *(a.replace("{dir}", str(folder)) for a in check.args)]


def _load(state: Path) -> dict[str, Any]:  # Any: JSON values
    if not state.is_file():
        return {}
    data = json.loads(state.read_text(encoding="utf-8"))
    return dict(data) if isinstance(data, dict) else {}


def run_checks(  # noqa: PLR0913 - the folder, the floor and the injected process, clock and memory
    folder: Path,
    *,
    checks: Sequence[Check] = CHECKS,
    floor_mb: int = FLOOR_MB,
    run: Callable[[list[str], Path, dict[str, str]], int],
    read_free: Callable[[], int],
    clock: Callable[[], float] = time.monotonic,
    env: dict[str, str] | None = None,
    write: Callable[[str], object] = sys.stdout.write,
) -> int:
    """Run every check not yet done, in order; 0, or EXIT_LOW_MEMORY when a pre-flight (here or
    in a check) refused."""
    folder.mkdir(parents=True, exist_ok=True)
    state_file = folder / "state.json"
    done = _load(state_file)
    write("\n".join(plan_lines(checks, done, floor_mb)) + "\n")
    with (folder / "report.jsonl").open("a", encoding="utf-8") as report:
        for check in checks:
            if check.name in done:
                continue
            if check.args is None:
                report.write(json.dumps({"check": check.name, "status": "not_built"}) + "\n")
                continue
            write(f"-- {check.name}: unblocks {check.unblocks}; ~{check.estimate_min} min\n")
            try:
                preflight(floor_mb=floor_mb, read=read_free)
            except LowMemory as refused:
                report.write(json.dumps({"check": check.name, "status": "refused"}) + "\n")
                write(f"{refused}\nrun the same command again once memory is free\n")
                return EXIT_LOW_MEMORY
            started = clock()
            code = run(_argv(check, folder), folder / check.name, env or {})
            row = {"check": check.name, "exit": code, "seconds": round(clock() - started)}
            if code == EXIT_LOW_MEMORY:
                report.write(json.dumps({**row, "status": "refused"}) + "\n")
                return EXIT_LOW_MEMORY
            report.write(json.dumps({**row, "status": "finished"}) + "\n")
            report.flush()
            done[check.name] = code
            state_file.write_text(json.dumps(done), encoding="utf-8")
    return 0


def _run(argv: list[str], stem: Path, env: dict[str, str]) -> int:  # pragma: no cover - live
    with (
        stem.with_suffix(".out").open("w", encoding="utf-8") as out,
        stem.with_suffix(".err").open("w", encoding="utf-8") as err,
    ):
        return subprocess.run(argv, stdout=out, stderr=err, env=env, check=False).returncode  # noqa: S603 - our own modules, fixed lists


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, required=True, help="the report folder (resumable)")
    parser.add_argument("--min-free-mb", type=int, default=FLOOR_MB)
    parser.add_argument("--plan", action="store_true", help="print the plan and stop")
    args = parser.parse_args(argv)
    if args.plan:
        done = _load(args.dir / "state.json")
        sys.stdout.write("\n".join(plan_lines(CHECKS, done, args.min_free_mb)) + "\n")
        return 0
    return run_checks(
        args.dir,
        floor_mb=args.min_free_mb,
        run=_run,
        read_free=free_physical_mb,
        env=child_env(os.environ, ENV_FILE),
    )


if __name__ == "__main__":
    raise SystemExit(main())
