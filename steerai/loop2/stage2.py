"""Stage 2's harness (packet v2-ports-adapter, stage 2 plan rev 4.4 §2, [V2.63]): `--loop v2` on
the pooled set's real-site tasks, group 2b, each run classified and the stage accepted or not.

Stage 1's core is reused, not copied: `RunRecord`, `classify` (behind the site's own voids here),
`accept` with its void split and the 24-of-30 valid-task rule ([V2.63] Q5, #393), the child and
its cap, the precheck and its waits, and the working-set sampler. What is new:

- **The tasks are data.** `site_tasks` takes the pooled set's real-site rows (`task_suite`) and
  each one's expected outcome from the committed table (`stage2_expected`). No site is named here.
- **The start-page check** (read-only, before the child, so always before Steer acted):
  `site_down` (no 2xx/3xx main document, or Chrome's error page), `bot_wall` (a wall signature
  on the page), `start_page_differs` (the act's target is missing: a consent or region page),
  `site_changed` (the deciding facts differ from the table's, never a whole-page hash; Q4).
- **The site-void contract** ([V2.63] Q5, stage 1's docstring): a site void comes only from an
  observation independent of Steer's result: the start-page check, the harness's own read after
  the run showing a wall, or the site's control. Never from Steer's outcome.
- **A bot wall voids its site for the rest of the stage**, with no retry and no further control
  against it ([V2.63] Q4).
- **Per-site controls before each batch** (§2.4): a failed control voids that site's runs in the
  batch, which are never started. A check with no fixed page has no control (rev 4.4a ruling
  (c)): its runs are classified normally and the summary lists them as `no_control`.
- **Round robin with per-host spacing** (§2.3): run 1 of every task, then run 2, and so on; one
  host is never three in a row; every load of a host, controls included, waits `SITE_SPACING_S`
  after the last one. Re-runs (voids, monitor losses, runs not started) go in later batches,
  within stage 1's `MAX_RERUNS`.

Live: `uv run python -m steerai.loop2.stage2 --table <table.json> --runs 3 --out <dir>
--price-file <prices>` (Windows; real sites and real input through the child; only in a window
the coordinator has cleared).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from steerai.loop2.stage1 import (
    FLOOR_MB,
    MAX_RERUNS,
    SITE_VOIDS,
    TASK_CAP_S,
    Classified,
    RunRecord,
    StageTask,
    Summary,
    accept,
    child_argv,
    classify,
    precheck,
    run_child,
    run_sampled,
    site_voided,
    wait_until_ready,
)
from steerai.loop2.stage2_expected import (
    DAILY_CAP,
    START_EXPRESSION,
    DecidingFacts,
    SiteRow,
    Spacer,
    TableRow,
    deciding_facts,
    load_acts,
    load_table,
    load_unrated,
    real_site_tasks,
    site_check,
    start_document,
)
from steerai.probes.task_suite import CHALLENGE_TITLES, Task, control_url

ControlResult = Literal["pass", "fail", "wall"]
INVALID_RUN_VOIDS: Final[Mapping[str, str]] = {
    "page_not_loaded": "site_down",
    "bot_check": "bot_wall",
    "result_link_preexisting": "start_page_differs",
}
"""#403 blocker 2: a run that never started is a site void only by the harness's own token; any
other failure before the page read (launch, window, DevTools, UIA) is the machine's."""
STEER_CAUSED: Final = ("cap_killed", "no_final_line", "internal", "enters:")
"""Stage 1's results that are Steer's whatever the page shows: they come before any wall."""
MIN_VALID_TASKS: Final = 24
"""[V2.63] Q5: a conclusive stage needs at least this many of the 30 tasks not site-voided."""
LABEL: Final = "2b"


@dataclass(frozen=True)
class SiteTask:
    stage: StageTask
    host: str
    facts: DecidingFacts | None
    """The committed deciding facts the start page is checked against; None for a task held
    unrated ([V2.65] (2))."""
    controlled: bool = True
    """The task's check has a fixed page its control opens (rev 4.4a: False is `no_control`)."""
    suite: Task | None = None
    """The `task_suite` row: its check and the independent read (live only)."""
    row: SiteRow | None = None
    """Its acts, to read the start page's deciding facts (live only)."""


@dataclass(frozen=True)
class StartPage:
    """The start-page check's facts, read before the child starts."""

    loaded: bool
    """A 2xx or 3xx main document, and not Chrome's own error page."""
    wall: bool
    """A wall signature on the page (`task_suite.CHALLENGE_TITLES`)."""
    facts: DecidingFacts | None
    """The deciding facts on this read; None when the act's target is missing."""


@dataclass(frozen=True)
class SiteRecord:
    record: RunRecord
    start: str | None
    """The start-page check's void, if any (the child was never started)."""
    wall_host: str | None = None
    """The host of the page that showed a wall to the read after the run (that read is of the
    page Steer left, so it is not independent on its own: `classify_site`)."""


def host_of(task: Task) -> str:
    return urllib.parse.urlsplit(task.start).netloc


def site_tasks(
    table: Mapping[str, TableRow],
    acts: Mapping[str, SiteRow] | None = None,
    *,
    unrated: Mapping[str, str] | None = None,
) -> tuple[SiteTask, ...]:
    """Group 2b: every real-site task with its committed expected outcome. A task the table does
    not rate is a `ValueError` (`unrated`): no task runs without its outcome written first. A task
    held for the lead's ruling runs too, expected `unrated` ([V2.65] (2): held to the hard bar,
    out of the tasks gate), with no committed facts to check its start page against."""
    held = set(unrated or {})
    rated = [t for t in real_site_tasks() if t.task_id not in held]
    missing_rows = [t.task_id for t in rated if t.task_id not in table]
    if missing_rows:
        raise ValueError(f"unrated tasks: {missing_rows}")
    known = set(table) if acts is None else set(acts)
    missing = [t.task_id for t in rated if t.task_id not in known]
    if missing:
        raise ValueError(f"no_acts: {missing}")
    tasks = []
    for task in real_site_tasks():
        entry = table.get(task.task_id) if task.task_id not in held else None
        stage = StageTask(
            task.task_id,
            task.goal,
            "site",
            task.start,
            "unrated" if entry is None else entry.expected.outcome,
            reason=None if entry is None else entry.expected.reason,
        )
        tasks.append(
            SiteTask(
                stage,
                host_of(task),
                None if entry is None else entry.facts,
                controlled=control_url(task) is not None,
                suite=task,
                row=None if acts is None else acts.get(task.task_id),
            )
        )
    return tuple(tasks)


def start_void(start: StartPage, committed: DecidingFacts | None) -> str | None:
    """The start-page check's void, in its order: down, walled, the page not the expected one,
    then the deciding facts changed under the table. A task with no committed facts (held
    unrated) is checked only for down and walled."""
    if not start.loaded:
        return "site_down"
    if start.wall:
        return "bot_wall"
    if committed is None:
        return None
    if start.facts is None:
        return "start_page_differs"
    check = site_check(committed, start.facts)
    if check == "unread":
        return "start_page_unread"  # the harness's read failed: infrastructure, never the site's
    return "site_changed" if check == "changed" else None


def invalid_run_void(token: str) -> str | None:
    """#403 blocker 2: the site void a never-started run's `InvalidRun` token names; None for any
    other token (the run's void is then the machine's)."""
    return INVALID_RUN_VOIDS.get(token)


def harness_failed(task_id: str, run: int) -> RunRecord:
    """A run whose harness failed before the start page was read: stage 1's infrastructure void
    (`fixture_failed_before_start`)."""
    return RunRecord(task_id, run, None, False, False, None, None)


def _steer_caused(got: Classified) -> bool:
    return got.verdict == "fail" and (got.reason or "").startswith(STEER_CAUSED)


def needs_confirmation(task: SiteTask, site: SiteRecord) -> bool:
    """A wall after the run counts only on the task's own host and only over no Steer-caused
    result; only then is the harness's confirming load spent."""
    if site.start is not None or site.wall_host != task.host:
        return False
    return not _steer_caused(classify(task.stage, site.record))


def classify_site(task: SiteTask, site: SiteRecord, *, confirmed: bool = False) -> Classified:
    """The site's voids in front of stage 1's own rules, each from an observation independent of
    Steer's result ([V2.63] Q5). A wall after the run is the page Steer left, so it is a
    `bot_wall` only on the task's own host, over no Steer-caused result, and `confirmed` by a
    spaced, fresh-profile harness load (#403 blocker 1); otherwise the run is classified with its
    log unread."""
    record = site.record
    if site.start is not None:
        return Classified(record.task_id, record.run, "void", site.start, False, None, None)
    if confirmed and needs_confirmation(task, site):
        return Classified(
            record.task_id, record.run, "void", "bot_wall", False, record.final, record.memory
        )
    return classify(task.stage, record)


def interleaved(tasks: Iterable[SiteTask]) -> list[SiteTask]:
    """The batch's order: one task of each host in turn, so one host is never three in a row
    while another host has a task left."""
    by_host: dict[str, list[SiteTask]] = {}
    for task in tasks:
        by_host.setdefault(task.host, []).append(task)
    order: list[SiteTask] = []
    while any(by_host.values()):
        for queue in by_host.values():
            if queue:
                order.append(queue.pop(0))
    return order


def run_stage(  # noqa: PLR0913, PLR0915 - the tasks, then keyword-only knobs
    tasks: Sequence[SiteTask],
    *,
    runs: int,
    attempt: Callable[[SiteTask, int], SiteRecord | str],
    control: Callable[[str], ControlResult],
    confirm: Callable[[SiteTask], bool],
    spacer: Spacer,
    emit: Callable[[Mapping[str, Any]], None] = lambda _row: None,  # Any: JSON values
) -> tuple[list[Classified], int]:
    """Batches in round robin until each task has `runs` scored runs or `runs + MAX_RERUNS`
    attempts. Each batch first runs the control of each site with a task in the batch (a site
    whose tasks have none runs none); a failed one voids that site's runs in the batch,
    unstarted. A wall after a run is confirmed by `confirm`, a spaced harness load, when it would
    count. A bot wall (at the start, confirmed after a run, or on the control) voids its site for
    the rest of the stage: its tasks never run again, and each of them still short of its
    scored runs is `site_voided` (run 0). Returns the classified runs and how many were not
    started."""
    attempts = dict.fromkeys((t.stage.task_id for t in tasks), 0)
    scored = dict(attempts)
    walled: set[str] = set()
    controlled: dict[str, int] = {}  # host: the day of its last control
    voided: set[str] = set()  # hosts with a site void since their last control
    classified: list[Classified] = []
    not_started = 0
    while True:
        pending = [
            t
            for t in tasks
            if t.host not in walled
            and scored[t.stage.task_id] < runs
            and attempts[t.stage.task_id] < runs + MAX_RERUNS
        ]
        if not pending:
            break
        open_now = [t for t in pending if not spacer.at_cap(t.host)]
        if not open_now:
            spacer.wait_next_day()  # [V2.65] (3): every pending host is at its cap today
            continue
        due = _controls_due(
            open_now,
            attempts=attempts,
            scored=scored,
            controlled=controlled,
            voided=voided,
            today=spacer.day(),
        )
        failed, walls = _controls(due, control=control, spacer=spacer, emit=emit)
        for host in {t.host for t in due}:
            controlled[host] = spacer.day()
            voided.discard(host)
        walled |= walls
        for task in interleaved(open_now):
            task_id = task.stage.task_id
            if task.host in walled or spacer.at_cap(task.host):
                continue
            attempts[task_id] += 1
            run = attempts[task_id]
            if task.host in failed:
                got = Classified(task_id, run, "void", "site_control_failed", False, None)
            else:
                spacer.wait(task.host)
                result = attempt(task, run)
                if isinstance(result, str):  # never started: nothing was loaded
                    not_started += 1
                    emit({"task": task_id, "run": run, "not_started": result})
                    continue
                spacer.loaded(task.host)
                confirmed = False
                if needs_confirmation(task, result):
                    spacer.wait(task.host)
                    confirmed = confirm(task)
                    spacer.loaded(task.host)
                got = classify_site(task, result, confirmed=confirmed)
            classified.append(got)
            scored[task_id] += got.verdict not in ("void", "monitor_lost")
            emit({"task": task_id, "run": run, "verdict": got.verdict, "reason": got.reason,
                  "monitor_lost": got.monitor_lost, "memory": got.memory})  # fmt: skip
            if got.verdict == "void" and got.reason in SITE_VOIDS:
                voided.add(task.host)
            if got.verdict == "void" and got.reason == "bot_wall":
                walled.add(task.host)
    own_wall = {c.task_id for c in classified if c.verdict == "void" and c.reason == "bot_wall"}
    classified += [
        site_voided(t.stage)
        for t in tasks
        if t.host in walled and scored[t.stage.task_id] < runs and t.stage.task_id not in own_wall
    ]
    return classified, not_started


def _controls_due(  # noqa: PLR0913 - the batch, then each fact a control's repeat reads
    batch: Sequence[SiteTask],
    *,
    attempts: Mapping[str, int],
    scored: Mapping[str, int],
    controlled: Mapping[str, int],
    voided: set[str],
    today: int,
) -> list[SiteTask]:
    """[V2.65] (3): the batch's tasks whose host's control runs now: a host's first batch in the
    session, then only after a site void on it, or for a re-run (an earlier attempt not scored)
    on a later day than its last control."""
    due: list[SiteTask] = []
    for task in batch:
        host = task.host
        rerun = attempts[task.stage.task_id] > scored[task.stage.task_id]
        if host not in controlled or host in voided or (rerun and controlled[host] != today):
            due.append(task)
    return due


def _controls(
    pending: Sequence[SiteTask],
    *,
    control: Callable[[str], ControlResult],
    spacer: Spacer,
    emit: Callable[[Mapping[str, Any]], None],  # Any: JSON values
) -> tuple[set[str], set[str]]:
    """The batch's per-site controls (§2.4): the hosts whose control failed, and those whose
    control met a wall (#403 blocker 5: walled for the stage, no retry)."""
    failed: set[str] = set()
    walls: set[str] = set()
    for host in dict.fromkeys(t.host for t in interleaved(pending) if t.controlled):
        spacer.wait(host)
        result = control(host)
        spacer.loaded(host)
        emit({"control": host, "result": result})
        if result == "fail":
            failed.add(host)
        elif result == "wall":
            walls.add(host)
    return failed, walls


def volume_bound(tasks: Sequence[SiteTask], *, runs: int) -> dict[str, int]:
    """Per host, the most loads the stage can make before any wall confirmation ([V2.65] (3)):
    each task's runs and re-runs, a control in the host's first batch, and at most one more per
    re-run batch. Wall confirmations are not in it: at most one per attempt, and only after a
    wall on the task's own host."""
    bound: dict[str, int] = {}
    for task in tasks:
        bound[task.host] = bound.get(task.host, 0) + runs + MAX_RERUNS
    for host in {t.host for t in tasks if t.controlled}:
        bound[host] += 1 + MAX_RERUNS
    return bound


def days_bound(bound: Mapping[str, int]) -> dict[str, int]:
    """The days each host's bound needs at `DAILY_CAP` loads a day ([V2.65] (3))."""
    return {host: -(-loads // DAILY_CAP) for host, loads in bound.items()}


def summarize(  # noqa: PLR0913 - the runs, then keyword-only figures
    tasks: Sequence[SiteTask],
    classified: Sequence[Classified],
    *,
    not_started: int,
    runs_per_task: int,
    loads: Mapping[str, int] | None = None,
    unrated: Mapping[str, str] | None = None,
) -> Summary:
    """Stage 1's `accept` with stage 2's floor and valid-task minimum; the tasks with no control
    are listed apart (rev 4.4a (c)): unverifiable against a site change, never a scored pass."""
    summary = accept(
        [t.stage for t in tasks],
        classified,
        controls=None,  # the per-site controls void their batch instead (§2.4)
        runs_per_task=runs_per_task,
        label=LABEL,
        not_started=not_started,
        floor_mb=FLOOR_MB,
        min_valid_tasks=MIN_VALID_TASKS,
    )
    summary.report["no_control"] = [t.stage.task_id for t in tasks if not t.controlled]
    summary.report["unrated"] = dict(unrated or {})
    summary.report["harness_loads_per_host"] = dict(loads or {})
    summary.report["harness_loads_per_host_max"] = max((loads or {}).values(), default=0)
    bound = volume_bound(tasks, runs=runs_per_task)
    summary.report["harness_loads_bound"] = bound
    summary.report["harness_days_bound"] = days_bound(bound)
    return summary


# ---- the live run (Windows; real sites and real input through the child) ------------------------


def _start_page(site: SiteTask, port: int, hwnd: int) -> StartPage:  # pragma: no cover - live
    """The read-only start-page check: the main document over DevTools, then the deciding facts
    from a UIA full read of the window."""
    import secrets  # noqa: PLC0415 - live only

    from steerai.probes.shadow_v2 import _ComFacts  # noqa: PLC0415 - COM failures as OSError
    from steerai.probes.task_suite import _read  # noqa: PLC0415 - live only
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.world.model import WorldModel  # noqa: PLC0415 - Windows only
    from steerai.world.observe2 import read_observed  # noqa: PLC0415 - Windows only

    document = start_document(_read(port, START_EXPRESSION, "start"))
    loaded = document.loaded
    wall = document.title in CHALLENGE_TITLES
    if not loaded or wall or site.row is None or site.suite is None:
        return StartPage(loaded, wall, None)
    client = UiaClient()
    try:
        read = read_observed(
            WorldModel(client).observe(hwnd, cap_s=5.0),
            _ComFacts(client),
            key=secrets.token_bytes(32),
            windows=lambda: (hwnd,),
        )
    finally:
        del client  # its COM objects are released here, on this thread
    try:
        facts: DecidingFacts | None = deciding_facts(site.row, site.suite.goal, read)
    except ValueError:  # the act's target is missing: not the expected start page
        facts = None
    return StartPage(loaded, wall, facts)


def _live_site(  # noqa: PLR0911 - one return per way a run ends  # pragma: no cover - live
    site: SiteTask, run: int, *, out_dir: Path, price: Path
) -> SiteRecord:
    """One run: the start page in a throwaway Chrome, its read-only check, the child on its
    window, then the v1 suite's independent read and `passed` (no code shared with the
    verifier)."""
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.loop2.stage1 import _start, _tree_reading  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import CHROME_DEFAULT, page_ws_url  # noqa: PLC0415
    from steerai.probes.layer_b import WINDOW, _free_port  # noqa: PLC0415
    from steerai.probes.layer_b_pages import InvalidRun  # noqa: PLC0415
    from steerai.probes.suite_chunks import kill_tree  # noqa: PLC0415
    from steerai.probes.task_suite import (  # noqa: PLC0415 - live only
        SETTLE_LIMIT_S,
        _launch_one,
        _not_shown_before,
        _read,
        _read_page,
        _settled,
        _stop_one,
        passed,
    )
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    suite = site.suite
    if suite is None:
        raise ValueError("a live run needs the task's suite row")
    task_id = site.stage.task_id

    def unread(start: str) -> SiteRecord:
        return SiteRecord(RunRecord(task_id, run, None, True, False, None, None), start)

    def failed() -> SiteRecord:
        return SiteRecord(harness_failed(task_id, run), None)

    tmp = Path(tempfile.mkdtemp(prefix="steer-stage2-"))
    run_dir = out_dir / f"{task_id}-{run}"
    run_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    proc = _launch_one(CHROME_DEFAULT, tmp / "profile", port, suite.start, window_size=WINDOW)
    try:
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
        except (OSError, TimeoutError, RuntimeError):
            return failed()  # launch, window or DevTools: the machine's (#403 blocker 2)
        try:
            _settled(port, SETTLE_LIMIT_S)
            time.sleep(3.0)
            void = start_void(_start_page(site, port, window.hwnd), site.facts)
            if void is None:
                _not_shown_before(suite, port)
        except InvalidRun as exc:
            token = invalid_run_void(str(exc))
            return failed() if token is None else unread(token)
        except (OSError, TimeoutError, RuntimeError):
            return failed()  # a DevTools or UIA read failed: the machine's
        if void is not None:
            return unread(void)
        ran, memory = run_sampled(
            lambda: run_child(
                child_argv(site.stage, hwnd=window.hwnd, price=price,
                           trace=run_dir / "trace.jsonl"),
                cap_s=TASK_CAP_S, out=run_dir / "final.jsonl", start=_start, kill=kill_tree,
            ),
            read=_tree_reading,
        )  # fmt: skip

        def record(log_read: bool, check: bool | None) -> RunRecord:
            return RunRecord(task_id, run, ran.final, True, log_read, check, None,
                             killed=ran.killed, code=ran.code, memory=memory)  # fmt: skip

        try:
            left = json.loads(_read(port, _WALL_EXPRESSION, "after") or "{}")
            if str(left.get("title", "")) in CHALLENGE_TITLES:
                wall_host = urllib.parse.urlsplit(str(left.get("href", ""))).netloc
                return SiteRecord(record(False, None), None, wall_host=wall_host)
            observed = _read_page(port, "page", links=bool(suite.result_link))
        except (InvalidRun, OSError, RuntimeError, ValueError):
            return SiteRecord(record(False, None), None)
        return SiteRecord(record(True, passed(suite, "done", observed)), None)
    finally:
        _stop_one(proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)


_WALL_EXPRESSION: Final = "JSON.stringify({href: location.href, title: document.title})"


def _control(tasks: Sequence[SiteTask], host: str) -> ControlResult:  # pragma: no cover - live
    """The site's control this batch: its first controlled task's own control (`task_suite`); a
    bot check on it is a wall."""
    from steerai.probes.task_suite import controls  # noqa: PLC0415 - live only

    first = next(t for t in tasks if t.host == host and t.controlled and t.suite is not None)
    assert first.suite is not None  # noqa: S101 - narrowed by the filter above
    lines = controls([first.suite])
    line = lines[0] if lines else ""
    if "control passes" in line:
        return "pass"
    return "wall" if "(bot_check)" in line else "fail"


def _confirm_wall(site: SiteTask) -> bool:  # pragma: no cover - live
    """#403 blocker 1: a fresh-profile harness load of the task's control page (else its start
    page) shows the wall too."""
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes.chrome_a11y import CHROME_DEFAULT, page_ws_url  # noqa: PLC0415
    from steerai.probes.layer_b import WINDOW, _free_port  # noqa: PLC0415
    from steerai.probes.task_suite import (  # noqa: PLC0415 - live only
        CONTROL_ARGS,
        SETTLE_LIMIT_S,
        _launch_one,
        _read,
        _settled,
        _stop_one,
    )

    suite = site.suite
    if suite is None:
        return False
    url = control_url(suite) or suite.start
    tmp = Path(tempfile.mkdtemp(prefix="steer-wall-"))
    port = _free_port()
    proc = _launch_one(
        CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW, extra_args=CONTROL_ARGS
    )
    try:
        page_ws_url(port)
        _settled(port, SETTLE_LIMIT_S)
        time.sleep(4.0)
        page = json.loads(_read(port, _WALL_EXPRESSION, "confirm") or "{}")
        return str(page.get("title", "")) in CHALLENGE_TITLES
    except (OSError, TimeoutError, RuntimeError, ValueError):
        return False  # unconfirmed: the run is scored, never voided on a guess
    finally:
        _stop_one(proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live, Windows only
    from steerai.loop2.stage1 import _emit, _idle_s  # noqa: PLC0415 - live only
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m steerai.loop2.stage2")
    parser.add_argument("--table", type=Path, required=True, help="the committed expected table")
    parser.add_argument("--acts", type=Path, required=True, help="the acts data file")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--price-file", type=Path, required=True)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    acts = load_acts(args.acts.read_text(encoding="utf-8"))
    table_text = args.table.read_text(encoding="utf-8")
    unrated = load_unrated(table_text)  # [V2.65] (2): run, held to the hard bar
    tasks = site_tasks(load_table(table_text), acts, unrated=unrated)

    def attempt(site: SiteTask, run: int) -> SiteRecord | str:
        ready = wait_until_ready(
            lambda: precheck(free_mb=free_physical_mb, idle_s=_idle_s, floor_mb=FLOOR_MB),
            sleep=time.sleep,
            clock=time.monotonic,
        )
        if ready is not None:
            return ready
        return _live_site(site, run, out_dir=args.out, price=args.price_file)

    spacer = Spacer(time.monotonic, time.sleep)
    classified, not_started = run_stage(
        tasks,
        runs=args.runs,
        attempt=attempt,
        control=lambda host: _control(tasks, host),
        confirm=_confirm_wall,
        spacer=spacer,
        emit=lambda row: _emit({**row, "floor_mb": FLOOR_MB}),
    )
    summary = summarize(
        tasks,
        classified,
        not_started=not_started,
        runs_per_task=max(args.runs, 3),
        loads=spacer.loads,
        unrated=unrated,
    )
    _emit({"summary": {"accepted": summary.accepted, "gates": summary.gates, **summary.report}})
    return 0 if summary.accepted else 1


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
