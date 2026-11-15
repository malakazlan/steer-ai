"""Stage 1 (packet rev 4.3, §12, [V2.27], [V2.41], [V2.55], [V2.59]): `--loop v2` on local
fixtures only, N runs a task, each run classified and the set accepted or not.

Each run: a precheck (free RAM at least 4 GB, the person idle at least 2 s; else the run is not
started, said with its reason), the fixture served on 127.0.0.1 in a throwaway Chrome profile,
then the v2 CLI as a **child process** (`python -m steerai.run --loop v2 ...`). The child takes
the session and the hooks ([V2.58] (3)); this parent never does (an AST test pins it). The child
is capped and its process tree killed on the cap (v1's `suite_chunks.kill_tree`). Its final
JSON line is read back, then the fixture's own log over DevTools: the pass check shares no code
with the verifier.

A run is `pass`, `fail` (with its reason), `false_done` (Steer said done, the fixture says not),
`void` ([V2.41]: only what Steer did not cause, the fixture failing or its log unreadable
before Steer acted), or `monitor_lost` ([V2.59]: tallied apart). `accept` computes every gate
and every reported figure from the classified runs.

Live: `uv run python -m steerai.loop2.stage1 --set 3d --runs 3 --out <dir>` (Windows; sends
real input through the child; only in a window the coordinator has cleared with the person).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, get_args

from steerai.loop2.stage1_expected import LARGE_TASKS, Expected, large_expected
from steerai.probes.gallery_page import large_passes, parse_large_log
from steerai.probes.task_suite import GALLERY, h2_event

Expect = Literal["done", "blocked", "never_done", "not_done"]
Verdict = Literal["pass", "fail", "false_done", "void", "monitor_lost"]
Json = Mapping[str, Any]  # Any: a JSON object from the child's final line

FLOOR_MB: Final = 4096
"""The packet's preflight floor: a Chrome and a UIA walk need room ([V2.14] decision 4)."""
IDLE_FLOOR_S: Final = 2.0
"""The person must have been idle this long before a run starts (the probe's own gate)."""
TASK_CAP_S: Final = 600.0
"""The child's cap: twice the 300 s wall budget, so the budget stops the task first."""
INTERNAL: Final = frozenset({"internal_stub_reached", "internal_error"})
MIN_RUNS_PER_TASK: Final = 3
"""#366 P1: acceptance is 3 of 3 at least ([V2.27] stage 1, as v1's bar)."""
KILL_WAIT_S: Final = 10.0
"""How long the harness waits for a killed child's tree to go before it moves on."""
IDLE_WAIT_S: Final = 120.0
"""#366: how long a person at the keys is waited out before the run is not started."""
IDLE_POLL_S: Final = 2.0
MAX_RERUNS: Final = 3
"""#366 P1: a task's void, monitor_lost or not-started runs are re-run up to this many times."""
STATUSES: Final = frozenset({"done", "blocked", "asked", "paused", "stopped", "cannot", "error"})
"""The statuses the v2 CLI's final line can carry; anything else is not its line."""


@dataclass(frozen=True)
class StageTask:
    task_id: str
    goal: str
    fixture: str
    """Which local fixture serves it (`3d`, `gallery`, `large`)."""
    page: str
    """The served path the run starts on."""
    expect: Expect
    enters: int | None = None
    """When set, exactly this many Enter presses must reach the page (c-nobutton: 1)."""
    never_reason: str | None = None
    """When set, the run's reason must never be this one (c-nobutton: `unexpected_effect`)."""
    reason: str | None = None
    """#366 P4: when set, the run's reason must be this one (c-nobutton: `no_progress`, the
    runner's guard; a gallery row expected blocked: `commit_not_in_step`)."""


THREE_D_GOAL: Final = "search the catalog for red shoes"
THREE_D: Final = (
    StageTask("3d-a", THREE_D_GOAL, "3d", "/a", "done"),
    StageTask("3d-b", THREE_D_GOAL, "3d", "/b", "never_done"),
    StageTask("3d-c-button", THREE_D_GOAL, "3d", "/c-button", "done"),
    StageTask(
        "3d-c-nobutton",
        THREE_D_GOAL,
        "3d",
        "/c-nobutton",
        "not_done",
        enters=1,
        never_reason="unexpected_effect",
        reason="no_progress",
    ),
    StageTask("3d-d", THREE_D_GOAL, "3d", "/d", "done"),
)
"""§12's expected step-3 outcomes for the 3d variants (a, c-button, d done; b never done;
c-nobutton not done with exactly one Enter, never `unexpected_effect`)."""

GALLERY_PAGE: Final = "/page.html"
SETS: Final = ("3d", "gallery", "large")


def gallery_tasks(expected: Mapping[str, Expected]) -> tuple[StageTask, ...]:
    """The gallery rows as stage tasks, each with its expected outcome and reason
    (`stage1_expected`: rated by the gate on the gallery's saved read)."""
    goals = {t.task_id: t.goal for t in GALLERY}
    return tuple(
        StageTask(task_id, goals[task_id], "gallery", GALLERY_PAGE, e.outcome, reason=e.reason)
        for task_id, e in expected.items()
    )


def large_tasks() -> tuple[StageTask, ...]:
    """The `/large.html` tasks, expected done by ruling. Their acceptance stays fail-closed
    (`accept`'s `large_listed`, rev 4.3a) until the listed-when-acted check exists."""
    expected = large_expected()
    return tuple(
        StageTask(
            t.task_id,
            t.goal,
            "large",
            t.path,
            expected[t.task_id].outcome,
            reason=expected[t.task_id].reason,
        )
        for t in LARGE_TASKS
    )


def tasks_for(name: str, expected: Mapping[str, Expected]) -> tuple[StageTask, ...]:
    """The tasks of a set by its `--set` name; `expected` is the gallery's rated table."""
    if name == "3d":
        return THREE_D
    if name == "gallery":
        return gallery_tasks(expected)
    if name == "large":
        return large_tasks()
    raise ValueError(f"unknown set: {name}")


def page_check(task: StageTask, raw: str) -> bool:
    """The page's own pass check on its log as read over DevTools; shares no code with the v2
    loop or the verifier. Gallery: the one trusted event the row expects (v1's suite check);
    large: `gallery_page.large_passes`. A large log of another shape is a `ValueError`."""
    if task.fixture == "gallery":
        row = next(t for t in GALLERY if t.task_id == task.task_id)
        return h2_event(raw).strip() == row.expected
    if task.fixture == "large":
        for large in LARGE_TASKS:
            if large.task_id == task.task_id:
                return large_passes(large.task_id, parse_large_log(raw))
    raise ValueError(f"no page check for {task.fixture} task {task.task_id}")


@dataclass(frozen=True)
class RunRecord:
    """What one run left: the child's final line and the fixture's own facts."""

    task_id: str
    run: int
    final: Json | None
    fixture_ok: bool
    """The fixture was served and ready before the child started."""
    log_read: bool
    check: bool | None
    """The fixture's own pass check (None when the log was not read)."""
    enters: int | None
    killed: bool = False
    """The child hit its cap and its tree was killed (#366)."""
    code: int | None = None
    """The child's exit code; None when killed."""


@dataclass(frozen=True)
class Classified:
    task_id: str
    run: int
    verdict: Verdict
    reason: str | None
    monitor_lost: bool
    final: Json | None


def classify(  # noqa: PLR0911, PLR0912 - one return per rule, in the rules' order
    task: StageTask, record: RunRecord
) -> Classified:
    """One run's verdict ([V2.41] void, [V2.59] monitor_lost, false_done, the expected outcome)."""

    def verdict(v: Verdict, reason: str | None = None) -> Classified:
        lost = v == "monitor_lost"
        return Classified(record.task_id, record.run, v, reason, lost, record.final)

    # #366 P2: the rules in this order
    if not record.fixture_ok:
        return verdict("void", "fixture_failed_before_start")
    if record.killed:
        return verdict("fail", "cap_killed")
    final = record.final
    if final is None or final.get("status") not in STATUSES:  # #366 E2: the CLI's own line
        return verdict("fail", "no_final_line")
    if final.get("detail") == "internal_error" or final.get("reason") in INTERNAL:
        return verdict("fail", "internal")
    if final.get("status") == "error":
        return verdict("fail", f"start_refused:{final.get('detail')}")
    if not isinstance(final.get("used"), Mapping):  # LiveWorld refused to start: no use at all
        return verdict("fail", f"start_refused:{final.get('code')}")
    status, reason = final.get("status"), final.get("reason")
    if status == "done":  # done is never void and never hidden behind a lost monitor
        if not record.log_read:
            return verdict("fail", "done_log_unread")
        if record.check is not True:
            return verdict("false_done", reason)
    if record.log_read and task.enters is not None and record.enters != task.enters:
        return verdict("fail", f"enters:{record.enters}")
    if final.get("monitor_lost") is True:
        return verdict("monitor_lost", "monitor_lost")
    if not record.log_read:
        acted = _actions(final) != 0
        return (
            verdict("fail", "log_unreadable_after_act")
            if acted
            else verdict("void", "log_unreadable")
        )
    if task.never_reason is not None and reason == task.never_reason:
        return verdict("fail", f"reason:{reason}")
    if task.reason is not None and reason != task.reason:
        return verdict("fail", f"reason:{reason}")
    met = {
        "done": status == "done",
        "blocked": status == "blocked",
        "never_done": status != "done",
        "not_done": status != "done",
    }[task.expect]
    return verdict("pass") if met else verdict("fail", f"expected_{task.expect}:{status}:{reason}")


def _actions(final: Json) -> int | None:
    """Actions the run dispatched; None when unread (counted as acted: never a void)."""
    used = final.get("used")
    count = used.get("max_actions") if isinstance(used, Mapping) else None
    return count if isinstance(count, int) and not isinstance(count, bool) else None


@dataclass(frozen=True)
class Summary:
    accepted: bool
    gates: dict[str, bool]
    report: dict[str, Any] = field(default_factory=dict)  # Any: JSON figures


def accept(  # noqa: PLR0913 - the runs and every keyword-only count the gates read
    tasks: Sequence[StageTask],
    runs: Sequence[Classified],
    *,
    controls: tuple[int, int] | None,
    runs_per_task: int,
    label: str,
    not_started: int = 0,
) -> Summary:
    """Packet rev 4.3 §3: every gate from the classified runs, and every reported figure.

    `controls` is (passed, total) of the 3d positive controls, None when no 3d task ran. A task
    passes only with `runs_per_task` passes (#366 P1: 3 of 3; void, monitor_lost and
    not-started runs are re-run, and still count in the 1-in-10 rates). `label` names the set,
    so a partial set is never read as stage 1."""
    if runs_per_task < MIN_RUNS_PER_TASK:
        raise ValueError(f"acceptance needs at least {MIN_RUNS_PER_TASK} runs per task")
    total = len(runs)
    verdicts = {v: sum(r.verdict == v for r in runs) for v in get_args(Verdict)}
    per_task: dict[str, list[Verdict]] = {t.task_id: [] for t in tasks}
    for r in runs:
        per_task.setdefault(r.task_id, []).append(r.verdict)
    # false_done has its own gate; void and monitor_lost are not scored ([V2.41], [V2.59])
    scored = {k: [v for v in vs if v in ("pass", "fail")] for k, vs in per_task.items()}
    counted = [r for r in runs if r.final is not None and r.final.get("status") != "error"]
    finals = [r.final for r in counted if r.final is not None]
    gates = {
        "controls": controls is None or (controls[1] > 0 and controls[0] == controls[1]),
        "tasks": all(vs.count("pass") == runs_per_task == len(vs) for vs in scored.values()),
        "false_done": verdicts["false_done"] == 0,
        # a run not started was not Steer's either: it counts with the voids ([V2.41])
        "void": (verdicts["void"] + not_started) * 10 <= total + not_started,
        "monitor_lost": verdicts["monitor_lost"] * 10 <= total,
        "cache_zero": not any(_cache_spent(r.final) for r in counted),
        "internal": not any(r.reason == "internal" for r in runs),
        # #366 P5: the target listed on the turn it is acted on is not checked yet: fail closed
        "large_listed": not any(t.fixture == "large" for t in tasks),
    }
    report = {
        "gate_failures": _gate_failures(gates, tasks, runs, scored, runs_per_task),
        "set": label,
        "runs_per_task": runs_per_task,
        "not_started": not_started,
        **_report(runs, finals, verdicts, per_task, total),
    }
    return Summary(all(gates.values()), gates, report)


def _cache_spent(final: Json | None) -> bool:
    """[V2.45]: caching is off in stage 1. A counter above zero, or a line with no counters at
    all, is a failure; a run that ended with its counters unknown (all null: the CLI's
    unfinished shape, #366) already failed on its own and is not a second failure here."""
    cache = None if final is None else final.get("cache_tokens")
    if not isinstance(cache, Mapping):
        return True
    values = [cache.get(k) for k in ("write_5m", "write_1h", "read")]
    if all(v is None for v in values):
        return False
    return any(v != 0 for v in values)


def _counted(run: Classified) -> bool:
    """A run whose final line is a task's own (not a refused start)."""
    return run.final is not None and run.final.get("status") != "error"


def _gate_failures(
    gates: Mapping[str, bool],
    tasks: Sequence[StageTask],
    runs: Sequence[Classified],
    scored: Mapping[str, Sequence[str]],
    runs_per_task: int,
) -> dict[str, list[str]]:
    """#366: each failed gate with the runs (`task#run`) or tasks that failed it."""

    def ids(match: Callable[[Classified], bool]) -> list[str]:
        return [f"{r.task_id}#{r.run}" for r in runs if match(r)]

    named = {
        "false_done": ids(lambda r: r.verdict == "false_done"),
        "void": ids(lambda r: r.verdict == "void"),
        "monitor_lost": ids(lambda r: r.verdict == "monitor_lost"),
        "cache_zero": ids(lambda r: _counted(r) and _cache_spent(r.final)),
        "internal": ids(lambda r: r.reason == "internal"),
        "tasks": [
            k for k, vs in scored.items() if not vs.count("pass") == runs_per_task == len(vs)
        ],
        "large_listed": [t.task_id for t in tasks if t.fixture == "large"],
        "controls": ["controls"],
    }
    return {gate: named.get(gate, []) for gate, ok in gates.items() if not ok}


def _report(
    runs: Sequence[Classified],
    finals: Sequence[Json],
    verdicts: Mapping[str, int],
    per_task: Mapping[str, Sequence[str]],
    total: int,
) -> dict[str, Any]:  # Any: JSON figures
    used_keys = ("max_model_calls", "max_actions", "max_tokens", "max_wall_s", "max_cost_usd")
    used = {k: [_number(f, "used", k) for f in finals] for k in used_keys}
    listings = [t for f in finals for t in f.get("listings", []) if isinstance(t, Mapping)]
    focus_keys = ("observations_focus_unread", "observations_focus_elsewhere", "type_unfocused",
                  "focus_moved_after_press")  # fmt: skip
    walls = [w for w in used["max_wall_s"] if w is not None]
    costs = [c for c in used["max_cost_usd"] if c is not None]
    return {
        "runs": total,
        "verdicts": dict(verdicts),
        "per_task": {
            k: {"pass": sum(v == "pass" for v in vs), "runs": len(vs)} for k, vs in per_task.items()
        },
        "paused_by": _counts(f.get("paused_by") for f in finals if f.get("paused_by")),
        "used_p50": {k: _quantile(v, 0.5) for k, v in used.items()},
        "used_p95": {k: _quantile(v, 0.95) for k, v in used.items()},
        "listing": {
            "turns": len(listings),
            "eligible_max": max((_whole(t.get("eligible")) or 0 for t in listings), default=None),
            "cut_total": sum(_whole(t.get("cut")) or 0 for t in listings),
            "prompt_cut_total": sum(_whole(t.get("prompt_cut")) or 0 for t in listings),
            "prompt_tokens_p95": _quantile([t.get("prompt_tokens") for t in listings], 0.95),
            "incomplete": sum(  # #366 E10: a turn with any count unread or not a number
                any(_whole(t.get(k)) is None for k in LISTING_COUNTS) for t in listings
            ),
        },
        "focus": {k: sum(int(_number(f, "focus", k) or 0) for f in finals) for k in focus_keys},
        "hook_latency_p95_ms_max": _max(_number(f, "world", "hook_latency_p95_ms") for f in finals),
        "hook_latency_p99_ms_max": _max(_number(f, "world", "hook_latency_p99_ms") for f in finals),
        "wall_s_p50": _quantile(walls, 0.5),
        "wall_s_p95": _quantile(walls, 0.95),
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "void_rate": round(verdicts.get("void", 0) / total, 3) if total else None,
        "monitor_lost_rate": round(verdicts.get("monitor_lost", 0) / total, 3) if total else None,
        "fail_reasons": _counts(r.reason for r in runs if r.verdict == "fail"),
    }


def _number(final: Json, outer: str, key: str) -> float | None:
    block = final.get(outer)
    value = block.get(key) if isinstance(block, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


LISTING_COUNTS: Final = ("eligible", "shown", "cut", "prompt_cut", "prompt_tokens")


def _whole(value: object) -> int | None:
    """A count as the CLI writes it; None for anything else (NaN, None, a string, a bool)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if math.isfinite(value) else None


def _quantile(values: Sequence[object], q: float) -> float | None:
    numbers = sorted(
        float(v)
        for v in values
        if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
    )
    if not numbers:
        return None
    return round(numbers[min(len(numbers) - 1, math.ceil(q * len(numbers)) - 1)], 3)


def _max(values: Iterable[float | None]) -> float | None:
    numbers = [v for v in values if v is not None]
    return max(numbers) if numbers else None


def _counts(items: Iterable[object]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[str(item)] = out.get(str(item), 0) + 1
    return out


# ---- the re-run loop and the wait for the person (#366: pure, the attempt injected) ----------


def run_task_runs(
    task: StageTask,
    *,
    runs: int,
    attempt: Callable[[int], RunRecord | str],
    emit: Callable[[Mapping[str, Any]], None] = lambda _row: None,  # Any: JSON values
) -> tuple[list[Classified], int]:
    """One task's runs until `runs` are scored or `runs + MAX_RERUNS` were attempted: a void,
    a monitor_lost and a run not started (`attempt` returns its reason) are not scored and are
    re-run, within the bound; a task that runs out fails closed in `accept` (too few passes).
    Returns the classified runs and how many were not started."""
    classified: list[Classified] = []
    scored = not_started = run = 0
    while scored < runs and run < runs + MAX_RERUNS:
        run += 1
        record = attempt(run)
        if isinstance(record, str):
            not_started += 1
            emit({"task": task.task_id, "run": run, "not_started": record})
            continue
        got = classify(task, record)
        classified.append(got)
        scored += got.verdict not in ("void", "monitor_lost")
        emit({"task": got.task_id, "run": got.run, "verdict": got.verdict,
              "reason": got.reason, "monitor_lost": got.monitor_lost})  # fmt: skip
    return classified, not_started


def wait_until_ready(
    check: Callable[[], str | None],
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    bound_s: float,
) -> str | None:
    """#366: a person at the keys is waited out, up to `bound_s`, before a run counts as not
    started; low memory is not waited for. None when the run may start."""
    started = clock()
    while True:
        refused = check()
        if refused != "person_active" or clock() - started >= bound_s:
            return refused
        sleep(IDLE_POLL_S)


# ---- the precheck --------------------------------------------------------------------------------


def precheck(*, free_mb: Callable[[], int], idle_s: Callable[[], float]) -> str | None:
    """None to start the run; else the reason it is not started. An unread reading refuses."""
    try:
        if free_mb() < FLOOR_MB:
            return "low_memory"
    except OSError:
        return "low_memory"
    idle = idle_s()
    if not (math.isfinite(idle) and idle >= IDLE_FLOOR_S):
        return "person_active"
    return None


# ---- the child run -------------------------------------------------------------------------------


class Child(Protocol):
    pid: int

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class ChildRun:
    final: Json | None
    killed: bool
    code: int | None


def child_argv(task: StageTask, *, hwnd: int, price: Path, trace: Path) -> list[str]:
    """The v2 CLI for one task on the bound window: the child takes the session and the hooks."""
    return [
        sys.executable, "-m", "steerai.run", "--loop", "v2", "--goal", task.goal,
        "--hwnd", str(hwnd), "--price-file", str(price), "--trace", str(trace),
    ]  # fmt: skip


def run_child(
    argv: list[str],
    *,
    cap_s: float,
    out: Path,
    start: Callable[[list[str], Path], Child],
    kill: Callable[[int], None],
) -> ChildRun:
    """Run the child to its end or its cap (then its whole tree is killed), and read its final
    JSON line from `out`; a last line that is not one JSON object is no final line."""
    child = start(argv, out)
    killed = False
    try:
        code: int | None = child.wait(timeout=cap_s)
    except subprocess.TimeoutExpired:
        killed = True
        kill(child.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=KILL_WAIT_S)  # bounded: a tree that will not die is not waited on
        code = None
    return ChildRun(None if killed else _last_json(out), killed, code)


def _last_json(out: Path) -> Json | None:
    try:
        lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        final = json.loads(lines[-1])
    except ValueError:
        return None
    return final if isinstance(final, dict) else None


def _start(argv: list[str], out: Path) -> Child:  # pragma: no cover - live
    with out.open("w", encoding="utf-8") as handle:  # the child holds its own copy
        return subprocess.Popen(argv, stdout=handle, stderr=subprocess.DEVNULL)  # noqa: S603 - our own CLI, a fixed argv


# ---- the live run (Windows; real input through the child) ---------------------------------------


def _idle_s() -> float:  # pragma: no cover - live, Windows only
    import ctypes  # noqa: PLC0415 - Windows only
    from ctypes import wintypes  # noqa: PLC0415

    class LastInput(ctypes.Structure):
        _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))

    last = LastInput()
    last.cbSize = ctypes.sizeof(LastInput)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last)):
        return 0.0  # #366 E11: unread is not idle
    tick = int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFF_FFFF
    return float((tick - int(last.dwTime)) & 0xFFFF_FFFF) / 1000


def _live_3d(
    task: StageTask, run: int, *, out_dir: Path, price: Path
) -> RunRecord:  # pragma: no cover - live
    """One 3d run: the page in a throwaway Chrome, the child on its window, the page's own log."""
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.suite_chunks import kill_tree  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    variant = task.page.lstrip("/")
    tmp = Path(tempfile.mkdtemp(prefix="steer-stage1-"))
    run_dir = out_dir / f"{task.task_id}-{run}"
    run_dir.mkdir(parents=True, exist_ok=True)
    with PageServer(fixture.pages()) as server:
        port = fixture._free_port()
        base = f"http://127.0.0.1:{server.port}"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, base + task.page)
        try:
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                cdp, _ = _ours(port, server.port)
                fixture._wait_ready(cdp)
            except (OSError, TimeoutError, RuntimeError):
                return RunRecord(task.task_id, run, None, False, False, None, None)
            ran = run_child(
                child_argv(task, hwnd=window.hwnd, price=price, trace=run_dir / "trace.jsonl"),
                cap_s=TASK_CAP_S, out=run_dir / "final.jsonl", start=_start, kill=kill_tree,
            )  # fmt: skip
            time.sleep(fixture.SETTLE_S)
            try:
                entries = fixture.parse_log(fixture._read_log(cdp, base))
            except (OSError, RuntimeError, ValueError):
                return RunRecord(task.task_id, run, ran.final, True, False, None, None,
                                 killed=ran.killed, code=ran.code)  # fmt: skip
            finally:
                cdp.close()
            enters = sum(e.ev == "enter" for e in entries)
            passed = fixture.passes(variant, entries)  # type: ignore[arg-type]  # a variant's path
            return RunRecord(task.task_id, run, ran.final, True, True, passed, enters,
                             killed=ran.killed, code=ran.code)  # fmt: skip
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


PAGE_SETTLE_S: Final = 1.0
"""How long a gallery or large run waits after the child before reading the page's log
(unmeasured: the 3d fixture's own settle)."""


def _live_page(
    task: StageTask, run: int, *, out_dir: Path, price: Path
) -> RunRecord:  # pragma: no cover - live
    """One gallery or large run: the gallery's pages in a throwaway Chrome, the child on its
    window, then the page's own log over DevTools (`page_check`)."""
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes import instant_search  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.gallery_page import LOG_KEY, pages  # noqa: PLC0415
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.suite_chunks import kill_tree  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    def evaluate(cdp: Any, expression: str) -> object:  # Any: a CdpSession
        reply = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return reply.get("result", {}).get("value")

    tmp = Path(tempfile.mkdtemp(prefix="steer-stage1-"))
    run_dir = out_dir / f"{task.task_id}-{run}"
    run_dir.mkdir(parents=True, exist_ok=True)
    with PageServer(pages()) as server:
        port = instant_search._free_port()
        proc = launch_chrome(
            CHROME_DEFAULT, tmp / "profile", port, f"http://127.0.0.1:{server.port}{task.page}"
        )
        try:
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                cdp, _ = _ours(port, server.port)
                deadline = time.monotonic() + 15.0
                while evaluate(cdp, "document.readyState") != "complete":
                    if time.monotonic() >= deadline:
                        raise TimeoutError("the page never finished loading")
                    time.sleep(0.1)
            except (OSError, TimeoutError, RuntimeError):
                return RunRecord(task.task_id, run, None, False, False, None, None)
            ran = run_child(
                child_argv(task, hwnd=window.hwnd, price=price, trace=run_dir / "trace.jsonl"),
                cap_s=TASK_CAP_S, out=run_dir / "final.jsonl", start=_start, kill=kill_tree,
            )  # fmt: skip
            time.sleep(PAGE_SETTLE_S)
            try:
                raw = evaluate(cdp, f'localStorage.getItem("{LOG_KEY}") || "[]"')
                if not isinstance(raw, str):
                    raise ValueError("the page log is not text")
                check = page_check(task, raw)
            except (OSError, RuntimeError, ValueError):
                return RunRecord(task.task_id, run, ran.final, True, False, None, None,
                                 killed=ran.killed, code=ran.code)  # fmt: skip
            finally:
                cdp.close()
            return RunRecord(task.task_id, run, ran.final, True, True, check, None,
                             killed=ran.killed, code=ran.code)  # fmt: skip
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


GALLERY_READ: Final = Path("tests/world/fixtures/gallery/page_read.json")
"""The gallery's saved read, relative to the repo root (`probes.gallery_read`)."""


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live, Windows only
    from steerai.loop2.stage1_expected import gallery_expected  # noqa: PLC0415 - live only
    from steerai.probes import instant_search  # noqa: PLC0415 - live only
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415
    from steerai.world.read_json import observed_from_json  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m steerai.loop2.stage1")
    parser.add_argument("--set", choices=SETS, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--price-file", type=Path, required=True)
    parser.add_argument("--gallery-read", type=Path, default=GALLERY_READ)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    expected = gallery_expected(observed_from_json(args.gallery_read.read_text("utf-8")))
    tasks = tasks_for(args.set, expected)
    controls: tuple[int, int] | None = None
    if args.set == "3d":
        passed = sum(
            instant_search.passes(v, instant_search.run_control(v)) for v in instant_search.VARIANTS
        )
        controls = (passed, len(instant_search.VARIANTS))
        _emit({"controls": {"passed": passed, "total": controls[1]}})
    live = _live_3d if args.set == "3d" else _live_page
    classified: list[Classified] = []
    not_started = 0
    for task in tasks:

        def attempt(run: int, task: StageTask = task) -> RunRecord | str:
            ready = wait_until_ready(
                lambda: precheck(free_mb=free_physical_mb, idle_s=_idle_s),
                sleep=time.sleep, clock=time.monotonic, bound_s=IDLE_WAIT_S,
            )  # fmt: skip
            if ready is not None:
                return ready
            return live(task, run, out_dir=args.out, price=args.price_file)

        runs, refused = run_task_runs(task, runs=args.runs, attempt=attempt, emit=_emit)
        classified += runs
        not_started += refused
    label = args.set if args.runs >= MIN_RUNS_PER_TASK else f"{args.set}-shakedown"
    summary = accept(tasks, classified, controls=controls,
                     runs_per_task=max(args.runs, MIN_RUNS_PER_TASK), label=label,
                     not_started=not_started)  # fmt: skip
    _emit({"summary": {"accepted": summary.accepted, "gates": summary.gates, **summary.report}})
    return 0 if summary.accepted else 1


def _emit(row: Mapping[str, Any]) -> None:  # Any: JSON values  # pragma: no cover - live
    sys.stdout.write(json.dumps(dict(row), allow_nan=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
