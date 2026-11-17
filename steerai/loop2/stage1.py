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

[V2.63] Q5, the contract for stage 2's classifier: a site void (`SITE_VOIDS`) comes only from
observations independent of Steer's result, such as a wall signature on the page or a control
probe run before the task, never from Steer's own outcome. A site void keeps a task out of the
tasks gate only when none of its runs scored a fail or a false_done (a scored failure is never
hidden behind one), and every site-void reason's rate is reported so over-assignment shows.

Live: `uv run python -m steerai.loop2.stage1 --set 3d --runs 3 --out <dir>` (Windows; sends
real input through the child; only in a window the coordinator has cleared with the person).
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, get_args

from steerai.errors import START_DETAILS, ErrorCode
from steerai.loop2.guards import StrikeReason
from steerai.loop2.history import REFUSAL_TOKENS
from steerai.loop2.loop import (
    AUDIT_COUNTS,
    INTAKE_COUNTS,
    PARENT_KINDS,
    REASON_TOKEN,
    URL_READS,
    RunnerAuditReason,
    SufficiencyReason,
)
from steerai.loop2.stage1_expected import LARGE_TASKS, Expected, large_expected
from steerai.orchestrator.contracts import RISKS
from steerai.probes.gallery_page import large_passes, parse_large_log
from steerai.probes.process_tree import Sample, summarize
from steerai.probes.task_suite import GALLERY, h2_event

Expect = Literal["done", "blocked", "never_done", "not_done", "unrated"]
"""`unrated` ([V2.65] (2)): a task held for the lead's ruling still runs and still meets the hard
bar (false_done, no dispatch at R2 or above); only its expected outcome is not checked."""
Verdict = Literal["pass", "fail", "false_done", "void", "monitor_lost"]
Json = Mapping[str, Any]  # Any: a JSON object from the child's final line

FLOOR_MB: Final = 4096
"""The packet's preflight floor: a Chrome and a UIA walk need room ([V2.14] decision 4). Stage 2
keeps it until a heavy real page is measured under the sampler ([V2.63])."""
STAGE1_FLOOR_MB: Final = 3072
"""[V2.63]: stage 1 (local fixtures only) runs on this floor; the browser tree peaked near
1.1 GB. Recorded on every row, with the working-set sampler on (`run_sampled`)."""
SAMPLE_EVERY_S: Final = 0.5
"""The sampler's period, as `probes.working_set`."""
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
MEMORY_WAIT_S: Final = 60.0
"""How long free memory below `FLOOR_MB` is waited out before the run is not started (the
coordinator's bound, 2026-11-15: the box idles at 4.2-4.6 GB free and dips under the floor for a
few seconds after Chrome processes close). The floor itself is the user's rule and stays."""
MEMORY_POLL_S: Final = 2.0
WAITED: Final[Mapping[str, tuple[float, float]]] = {
    "person_active": (IDLE_WAIT_S, IDLE_POLL_S),
    "low_memory": (MEMORY_WAIT_S, MEMORY_POLL_S),
}
"""The refusals a run waits out, each with its bound and its poll; any other is not waited for."""
EXIT_WAIT_S: Final = 30.0
"""How long the first task waits for the controls' own Chrome processes to exit (unmeasured;
stop_chrome already killed their tree and waited up to 10 s for the browser)."""
EXIT_POLL_S: Final = 0.5
MAX_RERUNS: Final = 3
"""#366 P1: a task's void, monitor_lost or not-started runs are re-run up to this many times."""
SITE_VOIDS: Final = frozenset(
    {"site_down", "start_page_differs", "site_changed", "bot_wall", "site_control_failed"}
)
"""[V2.63] Q5: voids that are the site's, not Steer's or the machine's: reported apart, never in
the 1-in-10 void rate. Every other void (and a run not started) is infrastructure. `site_down`
and `start_page_differs` are the site's too (the coordinator's note under [V2.63], 2026-11-15),
decided only from the harness's own read before the run."""
NO_RETRY_VOIDS: Final = frozenset({"bot_wall"})
"""[V2.63] Q4: a bot wall voids its site for the rest of the stage, with no retry against it."""
STATUSES: Final = frozenset({"done", "blocked", "asked", "paused", "stopped", "cannot", "error"})
LossVerdict = Literal["slow", "gone"]
"""[V2.64] Two: the `loss_watch` line's closed verdicts (an echo after the loss: slow; none in
the watch: gone); a monitor_lost run with no line is "unknown"."""
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
    max_enters: int | None = None
    """[V2.67] When set, at most this many Enter presses may reach the page (d: zero or one;
    whether the Enter is sent depends on timing, both paths are correct, never two)."""
    never_reason: str | None = None
    """When set, the run's reason must never be this one (c-nobutton: `unexpected_effect`)."""
    reason: str | None = None
    """#366 P4: when set, the run's reason must be this one (a gallery row expected blocked:
    `commit_not_in_step`)."""
    reasons: frozenset[str] = frozenset()
    """[V2.67] B: when set, the run's reason must be one of these (c-nobutton's endings)."""


NOBUTTON_REASONS: Final = frozenset(
    {"no_progress", "controller_stop", "after_refusal:press_not_at_focus"}
)
"""[V2.67] B: the closed endings c-nobutton may reach without acting further: the runner's
no-progress guard, the controller's own stop, or a press refused after its Enter (measured
2026-11-15: 3 of 3 runs ended on the last two)."""
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
        reasons=NOBUTTON_REASONS,
    ),
    StageTask("3d-d", THREE_D_GOAL, "3d", "/d", "done", max_enters=1),
)
"""The expected step-3 outcomes for the 3d variants (§12): a, c-button and d done; b never done;
c-nobutton not done with exactly one Enter, never `unexpected_effect`, ending on one of
`NOBUTTON_REASONS` ([V2.67] B); d with zero or one Enter, never two ([V2.67])."""

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


MTA_COMPARABILITY: Final[Mapping[str, Any]] = {  # Any: JSON values
    "status": "closed",
    "ruling": "[V2.65] Four",
    "asked_by": "[V2.64] Four",
    "probe": "probes.mta_act, 2026-11-15: v1's Session on the gallery's 11 widget rows, 3 runs "
    "per arm, its UIA client in the STA and forced into the MTA",
    "steps_ok": {"sta": "30/33", "mta": "30/33"},
    "trusted_in_every_run": 10,
    "differ": [],
    "g12": "explained: the probe's executor had no option source (#406, #414)",
    "artefact": "D:/steerai-data/probes/2026-11-15/mta_act.json",
    "artefact_summary": "local only, not in the repo; its summary verbatim in PR #400's body",
    "artefact_sha256": "02f8e6d1cbcfca359ecc8aa3ea143faab455d6b8c2d3975eaeea6527c9667e8a",
}
"""[V2.64] Four, closed by [V2.65] Four: v1's executors behave the same in both apartments, so
the stage 1 report states executor behaviour under the MTA as compared, not as unseparated."""


def report_header(name: str, *, runs: int) -> dict[str, Any]:  # Any: JSON values
    """The stage 1 report's first row: the set (a shakedown below `MIN_RUNS_PER_TASK` says so),
    the runs per task, the floor the runs start on and the MTA comparability statement."""
    return {
        "set": name if runs >= MIN_RUNS_PER_TASK else f"{name}-shakedown",
        "runs_per_task": runs,
        "floor_mb": STAGE1_FLOOR_MB,
        "mta_comparability": dict(MTA_COMPARABILITY),
    }


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


CHROME_LOST: Final = "chrome_lost"
"""#459: the run's own Chrome went away under the harness (its DevTools connection closed). With
no done claimed it is an infrastructure void, re-run within the bound and counted toward the
1-in-10 void limit; a done it leaves unread fails closed as `done_log_unread:chrome_lost`. The
same token as `chrome_a11y.CHROME_LOST`, which a test pins."""


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
    memory: Json | None = None
    """[V2.63]: the working-set sampler's summary over the child's run (`run_sampled`)."""
    loss_watch: LossVerdict | None = None
    """[V2.64] Two: the child's `loss_watch` line after a declared monitor loss; None: none."""
    kill_failure: str | None = None
    """#431: the cap's kill did not end the child (`ChildRun.kill_failure`)."""
    kill_after_final: bool = False
    """[V2.80]: the cap killed the child after its complete final line (`ChildRun`)."""
    lost: str | None = None
    """#459: the run's own Chrome went away under the harness (`CHROME_LOST`); None when not."""


@dataclass(frozen=True)
class Classified:
    task_id: str
    run: int
    verdict: Verdict
    reason: str | None
    monitor_lost: bool
    final: Json | None
    memory: Json | None = None
    loss_watch: LossVerdict | None = None
    """[V2.64] Two: "slow" or "gone"; None on a monitor_lost run is "unknown"."""
    kill_after_final: bool = False
    """[V2.80]: the cap killed the child after its complete final line (its loss watch ran on):
    the final line stands and the kill is recorded, never read as a run that did not finish."""


def classify(  # noqa: PLR0911, PLR0912 - one return per rule, in the rules' order
    task: StageTask, record: RunRecord
) -> Classified:
    """One run's verdict ([V2.41] void, [V2.59] monitor_lost, false_done, the expected outcome)."""

    def verdict(v: Verdict, reason: str | None = None) -> Classified:
        lost = v == "monitor_lost"
        return Classified(record.task_id, record.run, v, reason, lost, record.final, record.memory,
                          record.loss_watch, record.kill_after_final)  # fmt: skip

    # #366 P2: the rules in this order
    if not record.fixture_ok:
        return verdict("void", "fixture_failed_before_start")
    if record.killed and not (record.kill_after_final and record.kill_failure is None):
        failure = record.kill_failure
        return verdict("fail", "cap_killed" if failure is None else f"cap_killed:{failure}")
    final = record.final
    if final is None or final.get("status") not in STATUSES:  # #366 E2: the CLI's own line
        return verdict("fail", "no_final_line")
    if final.get("detail") == "internal_error" or final.get("reason") in INTERNAL:
        return verdict("fail", "internal")
    # [V2.67] C: a run that never began cannot be scored: an infrastructure void, its cause
    # split in the report and counted toward the 1-in-10 void limit (supersedes the fail rule)
    if not _started(final):  # refused before the task began: no use at all
        return verdict("void", _refused_start(final))
    if final.get("status") == "error":  # #438: an error after the task began stays Steer's
        return verdict("fail", "error_after_start")
    status, reason = final.get("status"), final.get("reason")
    if record.lost is not None:  # #459: the browser went away under the harness
        # a done that cannot be checked is where a false done would hide: it fails closed,
        # its cause kept; anything else is the machine's, a void (the coordinator's ruling)
        if status == "done":
            return verdict("fail", f"done_log_unread:{record.lost}")
        return verdict("void", record.lost)
    if status == "done":  # done is never void and never hidden behind a lost monitor
        if not record.log_read:
            return verdict("fail", "done_log_unread")
        if record.check is not True:
            return verdict("false_done", reason)
    if record.log_read and task.enters is not None and record.enters != task.enters:
        return verdict("fail", f"enters:{record.enters}")
    over = task.max_enters is not None and (record.enters or 0) > task.max_enters
    if record.log_read and over:
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
    if task.reason is not None and not reason_matches(task.reason, reason):
        return verdict("fail", f"reason:{reason}")
    if task.reasons and not any(reason_matches(r, reason) for r in task.reasons):
        return verdict("fail", f"reason:{reason}")
    met = {
        "done": status == "done",
        "blocked": status == "blocked",
        "never_done": status != "done",
        "not_done": status != "done",
        "unrated": True,  # any ending; a false done was caught above
    }[task.expect]
    return verdict("pass") if met else verdict("fail", f"expected_{task.expect}:{status}:{reason}")


REASON_CAP: Final = 80
"""A start refusal's reason, code and detail, cut to this many characters."""


def _refused_start(final: Json) -> str:
    """A refused start's reason: `start_refused:<code>` and its detail when it has one, as the
    child wrote them (the report keeps closed values only: `_closed_reason`)."""
    detail = final.get("detail")
    named = f":{detail}" if isinstance(detail, str) and detail else ""
    return f"start_refused:{final.get('code')}{named}"[:REASON_CAP]


def _started(final: Json | None) -> bool:
    """The child's line is a task's own (it has a `used` block): its world started. A start
    refusal ([V2.62]: the session, the window, the page) has none: an infrastructure void
    ([V2.67] C), never counted under a gate that reads a run's use."""
    return final is not None and isinstance(final.get("used"), Mapping)


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
    conclusive: bool = True
    """[V2.63] Q5: the void and monitor rates and the valid-task minimum held; when False the
    result is `inconclusive` (re-run the whole stage; keep this one), never a rejection."""


def accept(  # noqa: PLR0913 - the runs and every keyword-only count the gates read
    tasks: Sequence[StageTask],
    runs: Sequence[Classified],
    *,
    controls: tuple[int, int] | None,
    runs_per_task: int,
    label: str,
    not_started: int = 0,
    floor_mb: int | None = None,
    min_valid_tasks: int | None = None,
) -> Summary:
    """Packet rev 4.3 §3: every gate from the classified runs, and every reported figure.

    `controls` is (passed, total) of the 3d positive controls, None when no 3d task ran. A task
    passes only with `runs_per_task` passes (#366 P1: 3 of 3; void, monitor_lost and
    not-started runs are re-run, and still count in the 1-in-10 rates). `label` names the set,
    so a partial set is never read as stage 1.
    [V2.63] Q5: site voids are apart from the void rate; a task short of its counted runs
    through a site void is unmeasured (not valid, not failed); with `min_valid_tasks`, fewer
    valid tasks than that leave the stage inconclusive, as the void and monitor rates do."""
    if runs_per_task < MIN_RUNS_PER_TASK:
        raise ValueError(f"acceptance needs at least {MIN_RUNS_PER_TASK} runs per task")
    total = len(runs)
    verdicts = {v: sum(r.verdict == v for r in runs) for v in get_args(Verdict)}
    per_task: dict[str, list[Verdict]] = {t.task_id: [] for t in tasks}
    for r in runs:
        per_task.setdefault(r.task_id, []).append(r.verdict)
    # false_done has its own gate; void and monitor_lost are not scored ([V2.41], [V2.59])
    scored = {k: [v for v in vs if v in ("pass", "fail")] for k, vs in per_task.items()}
    site = [r for r in runs if r.verdict == "void" and r.reason in SITE_VOIDS]
    site_ids = {r.task_id for r in site}
    failed = {r.task_id for r in runs if r.verdict in ("fail", "false_done")}
    unmeasured = [
        k
        for k, vs in scored.items()
        if k in site_ids and k not in failed and len(vs) < runs_per_task
    ]
    infra = verdicts["void"] - len(site)
    held = {t.task_id for t in tasks if t.expect == "unrated"}  # [V2.65] (2): not exempt
    measured = {k: vs for k, vs in scored.items() if k not in unmeasured and k not in held}
    counted = [
        r
        for r in runs
        if r.final is not None and _started(r.final) and r.final.get("status") != "error"
    ]
    large_ids = {t.task_id for t in tasks if t.fixture == "large"}
    finals = [r.final for r in counted if r.final is not None]
    gates = {
        "controls": controls is None or (controls[1] > 0 and controls[0] == controls[1]),
        "tasks": all(vs.count("pass") == runs_per_task == len(vs) for vs in measured.values()),
        "false_done": verdicts["false_done"] == 0,
        # [V2.65] (2): the step 3 invariant over every run, unrated tasks' too; unread fails
        "no_r2_dispatch": not any(_r2_dispatched(r) for r in runs),
        # a run not started was not Steer's either: it counts with the voids ([V2.41])
        "void": (infra + not_started) * 10 <= total - len(site) + not_started,
        "monitor_lost": verdicts["monitor_lost"] * 10 <= total,
        "cache_zero": not any(_cache_spent(r.final) for r in counted),
        "internal": not any(r.reason == "internal" for r in runs),
        # #366 P5: the target listed on the turn it is acted on is not checked yet: fail closed
        "large_listed": all(_listed_when_acted(r.final) for r in counted if r.task_id in large_ids),
    }
    valid = len(per_task) - len(unmeasured)
    if min_valid_tasks is not None:
        gates["valid_tasks"] = valid >= min_valid_tasks
    conclusive = gates["void"] and gates["monitor_lost"] and gates.get("valid_tasks", True)
    accepted = all(gates.values())
    report = {
        "result": "inconclusive" if not conclusive else "accepted" if accepted else "rejected",
        "gate_failures": _gate_failures(
            gates, tasks, runs, measured, runs_per_task, unmeasured=unmeasured
        ),
        "set": label,
        "runs_per_task": runs_per_task,
        "not_started": not_started,
        **_report(runs, finals, verdicts, per_task, total),
        "memory": _memory(runs, floor_mb),
        "site_voids": _counts(r.reason for r in site),
        "site_void_rates": {  # report only: a classifier over-assigning site voids shows here
            k: round(n / total, 3) for k, n in _counts(r.reason for r in site).items()
        },
        "valid_tasks": valid,
        "site_invalid_tasks": unmeasured,
        "unrated_tasks": sorted(held),
        # #419: a gate failure on an unread count shows the run's own reason beside it
        "no_r2_dispatch_failures": [
            {
                "task": r.task_id,
                "run": r.run,
                "reason": _closed_reason(r.reason),
                "counts": _counts_state(r),
            }
            for r in runs
            if _r2_dispatched(r)
        ],
    }
    kept = total - len(site)
    report["void_rate"] = round(infra / kept, 3) if kept else None  # infrastructure only
    return Summary(accepted, gates, report, conclusive)


def _memory(runs: Sequence[Classified], floor_mb: int | None) -> dict[str, Any]:  # Any: JSON
    """[V2.63]: the floor the runs started on, and the sampler's worst readings over them."""
    sampled = [r.memory for r in runs if isinstance(r.memory, Mapping) and r.memory.get("samples")]
    return {
        "floor_mb": floor_mb,
        "runs_sampled": len(sampled),
        "peak_tree_ws_mb_max": _max(_finite(m.get("peak_tree_ws_mb")) for m in sampled),
        "min_free_mb_min": min(
            (f for m in sampled if (f := _whole(m.get("min_free_mb"))) is not None),
            default=None,
        ),
    }


def _listed_when_acted(final: Json | None) -> bool:
    """#366 P5: every listing turn that dispatched said all its targets were among the handles
    it showed, and a run that ended done dispatched under at least one. Anything unread (no
    rows, a missing or non-bool `acted_listed`, a count that is not whole) fails closed."""
    rows = None if final is None else final.get("listings")
    if not isinstance(rows, list):
        return False
    acted_any = False
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        acted = _whole(row.get("acted"))
        listed = row.get("acted_listed")
        if acted is None or not isinstance(listed, bool):
            return False
        if acted > 0:
            acted_any = True
            if not listed:
                return False
    return acted_any or final is None or final.get("status") != "done"


def _r2_dispatched(run: Classified) -> bool:
    """[V2.65] (2), every run (#413 blocker 2): the run dispatched an action at R2 or above, or
    what it dispatched is unread: fail closed. Only a void with no line (the child never ran: a
    fixture or start void) and a refused start (a void whose line has no use: the run never
    began) dispatched nothing; any other line, an internal error's among them, is checked."""
    final = run.final
    if run.verdict == "void" and (final is None or not _started(final)):
        return False
    counts = None if final is None else final.get("dispatch_risks")
    if not isinstance(counts, Mapping):
        return True
    exact = {r: counts.get(r) for r in RISKS}  # #413 blocker 3: exact non-negative ints
    if not all(type(n) is int and n >= 0 for n in exact.values()):
        return True
    return any(exact[r] != 0 for r in RISKS[RISKS.index("R2") :])


def _closed_reason(reason: str | None) -> str | None:  # noqa: PLR0911 - one return per field
    """#428 (2): a run's reason as closed tokens only. A reason that carries a field of the
    child's own line keeps it only when it is one of the closed values that field can hold (an
    error code, a count, a status, a reason token); anything else is `other`, so no page text
    can reach the report."""
    if reason is None or ":" not in reason:
        return reason
    head, _, tail = reason.partition(":")
    if head == "start_refused":
        code, sep, detail = tail.partition(":")
        if not sep:
            return reason if tail in _START_DETAILS else f"{head}:other"
        kept_code = code if code in _START_CODES else "other"
        kept_detail = detail if detail in START_DETAILS else "other"
        return f"{head}:{kept_code}:{kept_detail}"
    if head == "enters":
        return reason if tail.isdigit() or tail == "None" else f"{head}:other"
    if head == "done_log_unread":
        return reason if tail == CHROME_LOST else f"{head}:other"
    if head == "reason":
        return reason if _token(tail) else f"{head}:other"
    if head.startswith("expected_"):
        status, _, why = tail.partition(":")
        closed = status in STATUSES and (why == "None" or _token(why))
        return reason if closed else f"{head}:other"
    return "other"


def _token(text: str) -> bool:
    """A run's reason: a token, `after_refusal:<token>`, or `<class>:<token>` ([V2.70])."""
    parts = text.removeprefix("after_refusal:").split(":")
    return len(parts) <= 2 and all(REASON_TOKEN.fullmatch(p) is not None for p in parts)


def reason_matches(expected: str, got: object) -> bool:
    """[V2.70]: an expected ending scored by its reason class. Only `no_progress` has a class
    (its single cause, [V2.69] Two, is diagnostic detail): it matches `no_progress` and any
    `no_progress:<token>`. Every other expectation matches only itself (#460 review)."""
    if not isinstance(got, str):
        return False
    return got == expected or (expected == "no_progress" and got.startswith("no_progress:"))


_START_CODES: Final = frozenset({c.value for c in ErrorCode} | {"None"})
"""#438: the codes a refused start's line may carry: an error code, or none."""
_START_DETAILS: Final = frozenset(
    {c.value for c in ErrorCode} | START_DETAILS | {"internal_error", "None"}
)
"""The values a refused start's line carries that the report keeps: its code, a closed detail
the emitters share (`errors.START_DETAILS`, #429), or the CLI's own internal-error token."""


def _counts_state(run: Classified) -> str:
    """Why a run failed `no_r2_dispatch`: its counts were unread, or it dispatched at R2+."""
    counts = None if run.final is None else run.final.get("dispatch_risks")
    if not isinstance(counts, Mapping):
        return "unread"
    if not all(type(counts.get(r)) is int and counts.get(r, -1) >= 0 for r in RISKS):
        return "unread"
    return "r2_dispatched"


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


WATCH_CLASSES: Final = ("plain", "trigger", "submit", "wait")


def _watch_deadlines(finals: Sequence[Json]) -> dict[str, Any]:  # Any: JSON figures
    """[V2.48]: per watch class, the watches and those that ended at their deadline, summed over
    the runs; a count that is not a whole number is said (incomplete), never summed."""
    sums = {k: {"watches": 0, "deadline": 0} for k in WATCH_CLASSES}
    odd = 0
    for final in finals:
        block = final.get("watch_deadlines")
        if not isinstance(block, Mapping):
            continue
        for kind, counts in block.items():
            if kind not in sums or not isinstance(counts, Mapping):
                odd += 1
                continue
            for key in ("watches", "deadline"):
                value = _whole(counts.get(key))
                if value is None:
                    odd += 1
                else:
                    sums[kind][key] += value
    return {"watch_deadlines": sums, "watch_deadlines_incomplete": odd}


STRIKE_REASONS: Final = frozenset(get_args(StrikeReason))
COUNTED_FACTS: Final = ("stale_observations", "unread_automation_ids", "partial_page_calls")
OUTCOME_COUNTS: Final = (
    "second_toggles",
    "second_toggles_refused",
    "toggles_settled_on_reread",
    "fields_not_empty",
    "not_own_text",
    "evidence_taken",
    "rebased_open",
    "rebased_open_commit",
    "target_changes",
    "focus_losses",
    "history_dropped",
    "turn_trace_dropped",
    "turn_trace_errors",
    "pre_task_draft_clicks",
    *AUDIT_COUNTS,
    *INTAKE_COUNTS,
    "saved_calls",
)
"""[V2.63]: the final line's `counts` block, summed over the runs (its `submit_parents` by
`PARENT_KINDS`)."""


def _run_facts(finals: Sequence[Json]) -> dict[str, Any]:  # Any: JSON figures
    """Stage 2 plan §6 (3): the final lines' run facts over the runs. Counts are summed; the
    read cost's per-run summaries are combined without pretending to a pooled quantile (the
    median of the runs' p50, the largest p95 and max). As the watch deadlines: a count that is
    not a whole number, or a key that is not a closed token, is counted in
    `run_facts_incomplete`, never summed; a block that is missing or null (the unfinished line)
    is skipped."""
    odd = 0
    cost = {"observations": 0, "full_reads": 0}
    p50s: list[float] = []
    p95s: list[float] = []
    maxes: list[float] = []
    strike_count = 0
    by_reason = dict.fromkeys(sorted(STRIKE_REASONS), 0)
    counted = dict.fromkeys(COUNTED_FACTS, 0)
    urls: dict[str, int] = dict.fromkeys(URL_READS, 0)

    def whole(value: object) -> int | None:
        nonlocal odd
        got = _whole(value)
        odd += got is None
        return got

    def number(value: object, into: list[float]) -> None:
        nonlocal odd
        if value is None:
            return  # a run with no full read has no milliseconds: not odd
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            odd += 1
        else:
            into.append(float(value))

    def tally(block: object, into: dict[str, int], closed: Iterable[str]) -> None:
        nonlocal odd
        if not isinstance(block, Mapping):
            odd += block is not None
            return
        allowed = set(closed)
        for key, value in block.items():
            if key not in allowed:
                odd += 1
            elif (got := whole(value)) is not None:
                into[key] += got

    for final in finals:
        read = final.get("read_cost")
        if isinstance(read, Mapping):
            for key in ("observations", "full_reads"):
                if (got := whole(read.get(key))) is not None:
                    cost[key] += got
            number(read.get("p50_ms"), p50s)
            number(read.get("p95_ms"), p95s)
            number(read.get("max_ms"), maxes)
        struck = final.get("strikes")
        if isinstance(struck, Mapping):
            if (got := whole(struck.get("count"))) is not None:
                strike_count += got
            tally(struck.get("by_reason"), by_reason, STRIKE_REASONS)
        for key in COUNTED_FACTS:
            value = final.get(key)
            if value is not None and (got := whole(value)) is not None:
                counted[key] += got
        if final.get("url_reads") is not None:
            tally(final.get("url_reads"), urls, URL_READS)
    counts, counts_odd = _outcome_counts(finals)
    combobox, combobox_odd = _combobox_facts(finals)
    refusals, refusals_odd = _refusal_counts(finals)
    return {
        "read_cost": {
            **cost,
            "run_p50_ms_p50": _quantile(p50s, 0.5),
            "run_p95_ms_max": _max(p95s),
            "max_ms": _max(maxes),
        },
        "strikes": {"count": strike_count, "by_reason": {k: v for k, v in by_reason.items() if v}},
        **counted,
        "url_reads": urls,
        "refusals": refusals,
        "counts": counts,
        "combobox": combobox,
        "run_facts_incomplete": odd + counts_odd + combobox_odd + refusals_odd,
    }


def _refusal_counts(finals: Sequence[Json]) -> tuple[dict[str, int], int]:
    """[V2.46], [V2.63] (e): the final lines' `refusals` summed by token, with how many entries
    were odd (a token that is not a refusal token, or a count that is not a whole number):
    said, never summed; a missing or null block is skipped."""
    sums: dict[str, int] = {}
    odd = 0
    for final in finals:
        block = final.get("refusals")
        if not isinstance(block, Mapping):
            odd += block is not None
            continue
        for token, value in block.items():
            got = _whole(value)
            if token not in REFUSAL_TOKENS or got is None:
                odd += 1
            else:
                sums[token] = sums.get(token, 0) + got
    return {k: v for k, v in sums.items() if v}, odd


COMBOBOX_FACTS: Final = ("query_class_listed", "typed_into")


def _combobox_facts(finals: Sequence[Json]) -> tuple[dict[str, int], int]:
    """[V2.64] Two: how many runs held each ComboBox fact; a value that is not a bool is said
    (odd), never counted; a missing or null block is skipped."""
    held = dict.fromkeys(COMBOBOX_FACTS, 0)
    odd = 0
    for final in finals:
        block = final.get("combobox")
        if not isinstance(block, Mapping):
            odd += block is not None
            continue
        for key in COMBOBOX_FACTS:
            value = block.get(key)
            if not isinstance(value, bool):
                odd += 1
            else:
                held[key] += value
    return held, odd


def _outcome_counts(finals: Sequence[Json]) -> tuple[dict[str, Any], int]:  # Any: JSON figures
    """[V2.63]: the final lines' `counts` blocks summed, with how many values were odd (not a
    whole number, or a parent kind that is not one): said, never summed; a missing or null
    block is skipped."""
    sums = dict.fromkeys(OUTCOME_COUNTS, 0)
    parents = dict.fromkeys(PARENT_KINDS, 0)
    odd = 0
    for final in finals:
        block = final.get("counts")
        if not isinstance(block, Mapping):
            continue
        for key in OUTCOME_COUNTS:
            got = _whole(block.get(key))
            if got is None:
                odd += 1
            else:
                sums[key] += got
        kinds = block.get("submit_parents")
        if not isinstance(kinds, Mapping):
            odd += kinds is not None
            continue
        for kind, value in kinds.items():
            got = _whole(value)
            if kind not in parents or got is None:
                odd += 1
            else:
                parents[kind] += got
    return {**sums, "submit_parents": parents}, odd


def _counted(run: Classified) -> bool:
    """A run whose final line is a task's own (not a refused start)."""
    return run.final is not None and run.final.get("status") != "error"


def _gate_failures(  # noqa: PLR0913 - the gates and every list they name
    gates: Mapping[str, bool],
    tasks: Sequence[StageTask],
    runs: Sequence[Classified],
    scored: Mapping[str, Sequence[str]],
    runs_per_task: int,
    *,
    unmeasured: Sequence[str] = (),
) -> dict[str, list[str]]:
    """#366: each failed gate with the runs (`task#run`) or tasks that failed it."""

    def ids(match: Callable[[Classified], bool]) -> list[str]:
        return [f"{r.task_id}#{r.run}" for r in runs if match(r)]

    named = {
        "false_done": ids(lambda r: r.verdict == "false_done"),
        "void": ids(lambda r: r.verdict == "void" and r.reason not in SITE_VOIDS),
        "valid_tasks": list(unmeasured),
        "monitor_lost": ids(lambda r: r.verdict == "monitor_lost"),
        "cache_zero": ids(lambda r: _counted(r) and _cache_spent(r.final)),
        "internal": ids(lambda r: r.reason == "internal"),
        "tasks": [
            k for k, vs in scored.items() if not vs.count("pass") == runs_per_task == len(vs)
        ],
        "large_listed": ids(
            lambda r: (
                _counted(r)
                and r.task_id in {t.task_id for t in tasks if t.fixture == "large"}
                and not _listed_when_acted(r.final)
            )
        ),
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
            "goal_at_subcap": sum(t.get("goal_at_subcap") is True for t in listings),
            "root_depth_max": max(
                (d for t in listings if (d := _whole(t.get("root_depth"))) is not None),
                default=None,
            ),
            "root_depth_unread": sum(_whole(t.get("root_depth")) is None for t in listings),
            "prompt_bytes_p95": _quantile([t.get("prompt_bytes") for t in listings], 0.95),
            "prompt_bytes_max": max(
                (b for t in listings if (b := _whole(t.get("prompt_bytes"))) is not None),
                default=None,
            ),
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
        "cap_kills_after_final": sum(r.kill_after_final for r in runs),  # [V2.80]
        "monitor_losses": {  # [V2.64] Two: real against slow losses, from the loss watch
            kind: sum(
                r.verdict == "monitor_lost" and (r.loss_watch or "unknown") == kind for r in runs
            )
            for kind in ("slow", "gone", "unknown")
        },
        "fail_reasons": _counts(_closed_reason(r.reason) for r in runs if r.verdict == "fail"),
        "void_causes": _counts(_closed_reason(r.reason) for r in runs if r.verdict == "void"),
        "audit": _audits(finals),
        "start_nudge": _start_nudges(finals),
        **_watch_deadlines(finals),
        **_run_facts(finals),
    }


NUDGE_KINDS: Final = ("move1px", "minrestore")


def _start_nudges(finals: Sequence[Json]) -> dict[str, int]:
    """[V2.64] Three: how many runs' starts needed the one nudge, by kind, from the final
    line's `world` block; `none` when the start needed none, `unread` when the block or the
    key is missing or not a known kind."""
    counts = dict.fromkeys((*NUDGE_KINDS, "none", "unread"), 0)
    for final in finals:
        world = final.get("world")
        if not isinstance(world, Mapping) or "front_nudged" not in world:
            counts["unread"] += 1
            continue
        kind = world["front_nudged"]
        key = "none" if kind is None else kind if kind in NUDGE_KINDS else "unread"
        counts[key] += 1
    return counts


AUDIT_RESULTS: Final = ("allowed", "vetoed", "failed")
AUDIT_REASONS: Final = frozenset({*get_args(SufficiencyReason), *get_args(RunnerAuditReason)})
"""#425 (3): the closed audit reasons (the controller's verdicts and the runner's failures); any
other text is counted as `other`, so a model's words never become report keys."""


def _audits(finals: Sequence[Json]) -> dict[str, Any]:  # Any: JSON figures
    """[V2.64] One: the final audits over the runs: how many ran, by result and by reason. A
    block that is missing or of another shape is counted in `incomplete`, never guessed."""
    ran = incomplete = 0
    results: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for final in finals:
        block = final.get("audit")
        if not isinstance(block, Mapping) or not isinstance(block.get("ran"), bool):
            incomplete += 1
            continue
        if not block["ran"]:
            continue
        result, reason = block.get("result"), block.get("reason")
        if result not in AUDIT_RESULTS or not isinstance(reason, str):
            incomplete += 1
            continue
        ran += 1
        results[result] = results.get(result, 0) + 1
        key = reason if reason in AUDIT_REASONS else "other"
        reasons[key] = reasons.get(key, 0) + 1
    return {"ran": ran, "result": results, "reason": reasons, "incomplete": incomplete}


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


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


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


def run_task_runs(  # noqa: PLR0913 - the task, then keyword-only knobs
    task: StageTask,
    *,
    runs: int,
    attempt: Callable[[int], RunRecord | str],
    emit: Callable[[Mapping[str, Any]], None] = lambda _row: None,  # Any: JSON values
    row: Mapping[str, Any] | None = None,  # Any: JSON values
    classifier: Callable[[RunRecord], Classified] | None = None,
) -> tuple[list[Classified], int]:
    """One task's runs until `runs` are scored or `runs + MAX_RERUNS` were attempted: a void,
    a monitor_lost and a run not started (`attempt` returns its reason) are not scored and are
    re-run, within the bound; a task that runs out fails closed in `accept` (too few passes).
    Returns the classified runs and how many were not started. `row` joins every emitted row
    ([V2.63]: the floor the run started on). `classifier` replaces stage 1's `classify` (stage
    2's own void rules). A void in `NO_RETRY_VOIDS` ends the task's runs: no retry against a
    bot wall ([V2.63] Q4)."""
    facts = dict(row or {})
    classified: list[Classified] = []
    scored = not_started = run = 0
    while scored < runs and run < runs + MAX_RERUNS:
        run += 1
        record = attempt(run)
        if isinstance(record, str):
            not_started += 1
            emit({"task": task.task_id, "run": run, "not_started": record, **facts})
            continue
        got = classify(task, record) if classifier is None else classifier(record)
        classified.append(got)
        scored += got.verdict not in ("void", "monitor_lost")
        emit({"task": got.task_id, "run": got.run, "verdict": got.verdict,
              "reason": got.reason, "monitor_lost": got.monitor_lost, "memory": got.memory,
              **facts})  # fmt: skip
        if got.verdict == "void" and got.reason in NO_RETRY_VOIDS:
            break
    return classified, not_started


def site_voided(task: StageTask) -> Classified:
    """[V2.63] Q4: a task of a site already behind a bot wall, never run (run 0): void for the
    rest of the stage, unmeasured in the summary."""
    return Classified(task.task_id, 0, "void", "bot_wall", False, None)


def wait_until_ready(
    check: Callable[[], str | None],
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> str | None:
    """#366: a person at the keys, and free memory under the floor, are each waited out up to
    their bound in `WAITED` (from the start of the wait) before a run counts as not started; any
    other refusal is returned at once. None when the run may start. Kill-aware: a kill
    (KeyboardInterrupt) raised in a sleep ends the wait as itself, never as a run not started."""
    started = clock()
    while True:
        refused = check()
        waited = None if refused is None else WAITED.get(refused)
        if waited is None or clock() - started >= waited[0]:
            return refused
        sleep(waited[1])


def wait_for_exit(
    pids: set[int],
    *,
    alive: Callable[[], set[int]],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    bound_s: float = EXIT_WAIT_S,
) -> set[int]:
    """The harness's own processes in `pids` still alive after waiting up to `bound_s` for them
    to exit; empty when all have. Nothing is killed here (stop_chrome already did). Kill-aware
    as `wait_until_ready`."""
    started = clock()
    while True:
        left = pids & alive()
        if not left or clock() - started >= bound_s:
            return left
        sleep(EXIT_POLL_S)


# ---- the precheck --------------------------------------------------------------------------------


def precheck(
    *, free_mb: Callable[[], int], idle_s: Callable[[], float], floor_mb: int = FLOOR_MB
) -> str | None:
    """None to start the run; else the reason it is not started. An unread reading refuses."""
    try:
        if free_mb() < floor_mb:
            return "low_memory"
    except OSError:
        return "low_memory"
    idle = idle_s()
    if not (math.isfinite(idle) and idle >= IDLE_FLOOR_S):
        return "person_active"
    return None


# ---- the working-set sampler ([V2.63]) -----------------------------------------------------------

Reading = tuple[int, tuple[float, float, int]]
"""Free physical MB, and (summed working set MB, summed peak MB, processes) of a tree."""


def run_sampled[T](
    work: Callable[[], T],
    *,
    read: Callable[[], Reading],
    every_s: float = SAMPLE_EVERY_S,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[T, dict[str, Any]]:  # Any: JSON values
    """`work` with the sampler on: a reading before it, one every `every_s` while it runs (a
    daemon thread), one after; summarised as `probes.working_set` does. A reading that raises
    OSError is skipped, never guessed. A failure of `work` is its own and ends the sampling."""
    samples: list[Sample] = []
    lock = threading.Lock()
    started = clock()
    stop = threading.Event()

    def take() -> None:
        try:
            free, (total, peak, count) = read()
        except OSError:
            return
        with lock:
            samples.append(Sample(round(clock() - started, 2), free, total, peak, count))

    def loop() -> None:
        while not stop.wait(every_s):
            take()

    take()
    sampler = threading.Thread(target=loop, name="steer-stage1-sampler", daemon=True)
    sampler.start()
    try:
        result = work()
    finally:
        stop.set()
        sampler.join()
    take()
    return result, summarize(samples)


# ---- the child run -------------------------------------------------------------------------------


class Child(Protocol):
    pid: int

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class ChildRun:
    final: Json | None
    killed: bool
    code: int | None
    loss_watch: LossVerdict | None = None
    kill_after_final: bool = False
    """[V2.80]: killed at the cap with a complete final line already written."""
    kill_failure: str | None = None
    """#431: why the kill did not end the child (`kill_failed:<code>`, `kill_timeout`,
    `kill_raised:<type>`, `child_survived`); None when it did, or no kill was needed."""


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
    kill: Callable[[int], str | None],
) -> ChildRun:
    """Run the child to its end or its cap (then its whole tree is killed), and read its final
    JSON line from `out`, and its `loss_watch` line apart (`_read_out`: the final line is the
    last JSON object with a known CLI status, never just the last line). On any
    other exit path (a Ctrl+C, any exception while waiting) the child this call started is
    killed the same way before the exception goes on: a stopped run never leaves its task
    running. A kill that did not end the child is never a clean stop: on the cap it is the
    run's `kill_failure`; on another exit path it is a note on the original exception, which
    is re-raised as itself. A `start` that raises started nothing and kills nothing. Residual:
    a second Ctrl+C during the bounded kill wait interrupts that wait (the kill itself was
    already sent)."""
    child = start(argv, out)
    killed = False
    failure: str | None = None
    try:
        code: int | None = child.wait(timeout=cap_s)
    except subprocess.TimeoutExpired:
        killed = True
        failure = _end(child, kill)
        code = None
    except BaseException as exc:
        stopped = _end(child, kill)
        if stopped is not None:
            exc.add_note(f"child kill: {stopped}")
        raise
    final, watch = _read_out(out)  # [V2.80]: on the cap too; a cut-off line is no final line
    return ChildRun(
        final,
        killed,
        code,
        loss_watch=watch,
        kill_after_final=killed and final is not None,
        kill_failure=failure,
    )


def _end(child: Child, kill: Callable[[int], str | None]) -> str | None:
    """Kill the child's tree by the pid of the handle `start` gave, then wait for it, bounded
    by `KILL_WAIT_S`. None when the child ended; else why not (closed tokens): the kill's own
    report, `kill_raised:<type>` when the kill call raised, or `child_survived` when the child
    outlived the bounded wait."""
    try:
        failed = kill(child.pid)
    except Exception as exc:  # noqa: BLE001 - reported as a token, never masks the caller's stop
        failed = f"kill_raised:{type(exc).__name__}"
    try:
        child.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        return "child_survived"
    return failed


def _read_out(out: Path) -> tuple[Json | None, LossVerdict | None]:
    """The final line is the last JSON object with a known CLI status (`STATUSES`), never just
    the last line (the coordinator's ruling on [V2.64] Two); the `loss_watch` verdict is read
    apart, from the last line that carries one, and only from the closed set."""
    try:
        lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None, None
    final: Json | None = None
    watch: LossVerdict | None = None
    for line in lines:
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("status") in STATUSES:
            final = doc
        elif "loss_watch" in doc:
            block = doc["loss_watch"]
            verdict = block.get("verdict") if isinstance(block, dict) else None
            watch = "slow" if verdict == "slow" else "gone" if verdict == "gone" else None
    return final, watch


STDERR_NAME: Final = "stderr.dpapi"
"""[V2.67] C, #438: the child's stderr, kept beside its final line only protected (DPAPI, one
base64 line, as the debug trace): it can quote a title (a traceback's message, the CLI's list of
candidate windows), so it is never written in plaintext."""
STDERR_UNPROTECTED: Final = b"unprotected: dropped\n"
"""What the file holds when DPAPI failed: the stderr is dropped, never written clear."""
MAX_STDERR_BYTES: Final = 256 * 1024
"""The most stderr kept (the rest is dropped): a bound on memory, not a measured size."""
STDERR_JOIN_S: Final = 5.0
"""How long the reader may take to finish after the child ended (unmeasured)."""


class _GuardedChild:
    """A child whose stderr is read into memory as it comes (so the pipe never fills) and,
    once the child has ended, written protected beside its final line, exactly once."""

    def __init__(  # Any: a subprocess.Popen (its stderr a binary pipe), or a test's fake
        self, proc: Any, path: Path, protect: Callable[[bytes], bytes]
    ) -> None:
        self.pid = proc.pid
        self._proc, self._path, self._protect = proc, path, protect
        self._chunks: list[bytes] = []
        self._kept = False
        self._reader = threading.Thread(target=self._read, name="steer-stage1-stderr", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        pipe, size = self._proc.stderr, 0
        if pipe is None:
            return
        while chunk := pipe.read(4096):
            if size < MAX_STDERR_BYTES:
                self._chunks.append(chunk[: MAX_STDERR_BYTES - size])
                size += len(chunk)

    def wait(self, timeout: float | None = None) -> int:
        code = int(self._proc.wait(timeout=timeout))  # a timeout raises before anything is kept
        self._keep()
        return code

    def _keep(self) -> None:
        if self._kept:
            return
        self._kept = True
        self._reader.join(timeout=STDERR_JOIN_S)
        try:
            line = base64.b64encode(self._protect(b"".join(self._chunks))) + b"\n"
        except OSError:
            line = STDERR_UNPROTECTED
        self._path.write_bytes(line)


def _start(
    argv: list[str],
    out: Path,
    *,
    popen: Callable[..., Any] = subprocess.Popen,  # Any: a Popen or a test's fake
    protect: Callable[[bytes], bytes] | None = None,
) -> Child:
    """The child with its stdout to `out` and its stderr, protected, to `STDERR_NAME` beside
    it once it has ended (`_GuardedChild`). `protect` defaults to DPAPI for the current user."""
    if protect is None:
        from steerai.debug_trace import protect_for_user  # noqa: PLC0415 - DPAPI, Windows

        protect = protect_for_user
    with out.open("w", encoding="utf-8") as handle:  # the child holds its own copy
        proc = popen(argv, stdout=handle, stderr=subprocess.PIPE)
    return _GuardedChild(proc, out.with_name(STDERR_NAME), protect)


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


def _tree_reading() -> Reading:  # pragma: no cover - live, Windows only
    """Free memory, and the working set of this harness's own tree (the child, its Chrome)."""
    import os  # noqa: PLC0415

    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415
    from steerai.probes.process_tree import read_tree  # noqa: PLC0415

    return free_physical_mb(), read_tree(os.getpid())


def _live_3d(
    task: StageTask, run: int, *, out_dir: Path, price: Path
) -> RunRecord:  # pragma: no cover - live
    """One 3d run: the page in a throwaway Chrome, the child on its window, the page's own log."""
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        ChromeLost,
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
            ran, memory = run_sampled(
                lambda: run_child(
                    child_argv(task, hwnd=window.hwnd, price=price,
                               trace=run_dir / "trace.jsonl"),
                    cap_s=TASK_CAP_S, out=run_dir / "final.jsonl", start=_start, kill=kill_tree,
                ),
                read=_tree_reading,
            )  # fmt: skip
            time.sleep(fixture.SETTLE_S)
            try:
                entries = fixture.parse_log(fixture._read_log(cdp, base))
            except ChromeLost:  # #459: the browser went away; a void, never a crash
                return RunRecord(task.task_id, run, ran.final, True, False, None, None,
                                 killed=ran.killed, code=ran.code, memory=memory,
                                 kill_failure=ran.kill_failure, lost=CHROME_LOST,
                                 kill_after_final=ran.kill_after_final)  # fmt: skip
            except (OSError, RuntimeError, ValueError):
                return RunRecord(task.task_id, run, ran.final, True, False, None, None,
                                 killed=ran.killed, code=ran.code, memory=memory,
                                 kill_failure=ran.kill_failure,
                                 kill_after_final=ran.kill_after_final,
                                 loss_watch=ran.loss_watch)  # fmt: skip
            finally:
                cdp.close()
            enters = sum(e.ev == "enter" for e in entries)
            passed = fixture.passes(variant, entries)  # type: ignore[arg-type]  # a variant's path
            return RunRecord(task.task_id, run, ran.final, True, True, passed, enters,
                             killed=ran.killed, code=ran.code, memory=memory,
                                 kill_failure=ran.kill_failure,
                                 kill_after_final=ran.kill_after_final,
                                 loss_watch=ran.loss_watch)  # fmt: skip
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
        ChromeLost,
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
            ran, memory = run_sampled(
                lambda: run_child(
                    child_argv(task, hwnd=window.hwnd, price=price,
                               trace=run_dir / "trace.jsonl"),
                    cap_s=TASK_CAP_S, out=run_dir / "final.jsonl", start=_start, kill=kill_tree,
                ),
                read=_tree_reading,
            )  # fmt: skip
            time.sleep(PAGE_SETTLE_S)
            try:
                raw = evaluate(cdp, f'localStorage.getItem("{LOG_KEY}") || "[]"')
                if not isinstance(raw, str):
                    raise ValueError("the page log is not text")
                check = page_check(task, raw)
            except ChromeLost:  # #459: the browser went away; a void, never a crash
                return RunRecord(task.task_id, run, ran.final, True, False, None, None,
                                 killed=ran.killed, code=ran.code, memory=memory,
                                 kill_failure=ran.kill_failure, lost=CHROME_LOST,
                                 kill_after_final=ran.kill_after_final)  # fmt: skip
            except (OSError, RuntimeError, ValueError):
                return RunRecord(task.task_id, run, ran.final, True, False, None, None,
                                 killed=ran.killed, code=ran.code, memory=memory,
                                 kill_failure=ran.kill_failure,
                                 kill_after_final=ran.kill_after_final,
                                 loss_watch=ran.loss_watch)  # fmt: skip
            finally:
                cdp.close()
            return RunRecord(task.task_id, run, ran.final, True, True, check, None,
                             killed=ran.killed, code=ran.code, memory=memory,
                                 kill_failure=ran.kill_failure,
                                 kill_after_final=ran.kill_after_final,
                                 loss_watch=ran.loss_watch)  # fmt: skip
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


GALLERY_READ: Final = Path("tests/world/fixtures/gallery/page_read.json")
"""The gallery's saved read, relative to the repo root (`probes.gallery_read`)."""


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live, Windows only
    from steerai.loop2.stage1_expected import gallery_expected  # noqa: PLC0415 - live only
    from steerai.probes import instant_search  # noqa: PLC0415 - live only
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415
    from steerai.probes.process_tree import process_table, tree_pids  # noqa: PLC0415
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
    header = report_header(args.set, runs=args.runs)
    _emit({"header": header})
    controls: tuple[int, int] | None = None
    if args.set == "3d":
        ours: set[int] = set()

        def record(pid: int) -> None:  # the control's Chrome tree, read before it is stopped
            ours.update(tree_pids(pid, process_table()) or {pid})

        passed = sum(
            instant_search.passes(v, instant_search.run_control(v, on_stop=record))
            for v in instant_search.VARIANTS
        )
        controls = (passed, len(instant_search.VARIANTS))
        _emit({"controls": {"passed": passed, "total": controls[1]}})
        left = wait_for_exit(ours, alive=lambda: {pid for pid, _ in process_table()},
                             sleep=time.sleep, clock=time.monotonic)  # fmt: skip
        _emit({"controls_exited": not left, "controls_left": len(left)})
    live = _live_3d if args.set == "3d" else _live_page
    classified: list[Classified] = []
    not_started = 0
    for task in tasks:

        def attempt(run: int, task: StageTask = task) -> RunRecord | str:
            ready = wait_until_ready(
                lambda: precheck(free_mb=free_physical_mb, idle_s=_idle_s,
                                 floor_mb=STAGE1_FLOOR_MB),
                sleep=time.sleep, clock=time.monotonic,
            )  # fmt: skip
            if ready is not None:
                return ready
            return live(task, run, out_dir=args.out, price=args.price_file)

        runs, refused = run_task_runs(task, runs=args.runs, attempt=attempt, emit=_emit,
                                      row={"floor_mb": STAGE1_FLOOR_MB})  # fmt: skip
        classified += runs
        not_started += refused
    label = header["set"]
    summary = accept(tasks, classified, controls=controls,
                     runs_per_task=max(args.runs, MIN_RUNS_PER_TASK), label=label,
                     not_started=not_started, floor_mb=STAGE1_FLOOR_MB)  # fmt: skip
    _emit({"summary": {"accepted": summary.accepted, "gates": summary.gates, **summary.report}})
    return 0 if summary.accepted else 1


def _emit(row: Mapping[str, Any]) -> None:  # Any: JSON values  # pragma: no cover - live
    sys.stdout.write(json.dumps(dict(row), allow_nan=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
