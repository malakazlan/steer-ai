"""Stage 2's harness, the pure parts (stage 2 plan rev 4.4 §2, [V2.63]): the start-page check's
void rules and their order, the classification in front of stage 1's, the round robin over sites
with the per-host spacing on a fake clock, the per-site controls each batch, the bot wall that
voids a site for the rest of the stage, and the summary's 24-of-30 and 4096 MB floor."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2 import stage2
from steerai.loop2.stage1 import FLOOR_MB, MAX_RERUNS, RunRecord, StageTask
from steerai.loop2.stage1_expected import Expected
from steerai.loop2.stage2 import (
    MIN_VALID_TASKS,
    SITE_SPACING_S,
    SiteRecord,
    SiteTask,
    Spacer,
    StartPage,
    classify_site,
    interleaved,
    run_stage,
    site_tasks,
    start_void,
    summarize,
)
from steerai.loop2.stage2_expected import DecidingFacts, TableRow, real_site_tasks

FACTS = DecidingFacts("Edit", "searchbox", True, None, None, "R1")
OTHER = DecidingFacts("Edit", None, False, None, None, "R2")
DONE: Mapping[str, Any] = {"status": "done", "reason": None, "used": {"max_actions": 2}}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def site(task_id: str, host: str, *, controlled: bool = True) -> SiteTask:
    stage = StageTask(task_id, f"search {host}", "site", f"https://{host}/", "done")
    return SiteTask(stage, host, FACTS, controlled=controlled)


def record(task_id: str, run: int, *, final: Mapping[str, Any] | None = DONE) -> RunRecord:
    return RunRecord(task_id, run, final, True, True, True, None)


# ---- the start-page check (§2.2 step 3, [V2.63] Q4) ---------------------------------------------


def test_a_clean_start_page_is_no_void() -> None:
    assert start_void(StartPage(loaded=True, wall=False, facts=FACTS), FACTS) is None


@pytest.mark.parametrize(
    ("start", "void"),
    [
        (StartPage(loaded=False, wall=True, facts=None), "site_down"),
        (StartPage(loaded=True, wall=True, facts=FACTS), "bot_wall"),
        (StartPage(loaded=True, wall=True, facts=None), "bot_wall"),  # a wall hides the field
        (StartPage(loaded=True, wall=False, facts=OTHER), "site_changed"),
        (StartPage(loaded=True, wall=False, facts=None), "start_page_differs"),
    ],
)
def test_the_start_voids_in_their_order(start: StartPage, void: str) -> None:
    assert start_void(start, FACTS) == void


# ---- classification: the site's voids in front of stage 1's own --------------------------------


def test_a_start_void_is_the_runs_verdict_and_never_a_fail() -> None:
    got = classify_site(
        site("t1", "a.test").stage, SiteRecord(record("t1", 1, final=None), "site_changed")
    )
    assert (got.verdict, got.reason) == ("void", "site_changed")


def test_a_wall_on_the_independent_read_is_a_bot_wall() -> None:
    """An observation independent of Steer's result: the harness's own read shows the wall."""
    got = classify_site(
        site("t1", "a.test").stage, SiteRecord(record("t1", 1), None, wall_after=True)
    )
    assert (got.verdict, got.reason) == ("void", "bot_wall")


def test_with_no_site_void_stage_1s_rules_decide() -> None:
    got = classify_site(site("t1", "a.test").stage, SiteRecord(record("t1", 1), None))
    assert got.verdict == "pass"
    false = RunRecord("t1", 1, DONE, True, True, False, None)
    assert (
        classify_site(site("t1", "a.test").stage, SiteRecord(false, None)).verdict == "false_done"
    )


# ---- the spacing per host, on a fake clock ------------------------------------------------------


def test_a_second_load_of_a_host_waits_out_its_spacing() -> None:
    clock = Clock()
    spacer = Spacer(clock, clock.sleep)
    spacer.wait("a.test")
    spacer.loaded("a.test")
    clock.now += 10.0
    spacer.wait("b.test")  # another host: no wait
    assert clock.slept == []
    spacer.wait("a.test")
    assert clock.slept == [pytest.approx(SITE_SPACING_S - 10.0)]


def test_the_policy_numbers() -> None:
    assert SITE_SPACING_S == 60.0
    assert MIN_VALID_TASKS == 24
    assert FLOOR_MB == 4096


# ---- the round robin ----------------------------------------------------------------------------


def test_the_order_never_puts_one_host_three_in_a_row() -> None:
    tasks = [site("a1", "a.test"), site("a2", "a.test"), site("a3", "a.test"), site("b1", "b.test")]
    hosts = [t.host for t in interleaved(tasks)]
    assert sorted(hosts) == ["a.test"] * 3 + ["b.test"]
    assert not any(hosts[i] == hosts[i + 1] == hosts[i + 2] for i in range(len(hosts) - 2))


def _run(
    tasks: list[SiteTask],
    attempt: Any,  # Any: the test's attempt callable
    *,
    runs: int = 3,
    control: Any = lambda _host: True,  # Any: the test's control callable
) -> tuple[list[Any], int, list[tuple[str, str]], Clock]:
    clock = Clock()
    loads: list[tuple[str, str]] = []

    def logged_attempt(t: SiteTask, n: int) -> Any:
        loads.append(("run", t.stage.task_id))
        return attempt(t, n)

    def logged_control(host: str) -> bool:
        loads.append(("control", host))
        return bool(control(host))

    got, not_started = run_stage(
        tasks,
        runs=runs,
        attempt=logged_attempt,
        control=logged_control,
        spacer=Spacer(clock, clock.sleep),
    )
    return got, not_started, loads, clock


def test_run_one_of_every_task_comes_before_run_two_of_any() -> None:
    tasks = [site("a1", "a.test"), site("b1", "b.test")]
    got, _, loads, _ = _run(tasks, lambda t, n: SiteRecord(record(t.stage.task_id, n), None))
    runs = [(c.task_id, c.run) for c in got]
    assert runs == [("a1", 1), ("b1", 1), ("a1", 2), ("b1", 2), ("a1", 3), ("b1", 3)]
    assert loads[:2] == [("control", "a.test"), ("control", "b.test")]  # controls first, each batch


def test_every_load_of_a_host_is_spaced_and_controls_count_as_loads() -> None:
    tasks = [site("a1", "a.test")]
    _, _, _, clock = _run(tasks, lambda t, n: SiteRecord(record(t.stage.task_id, n), None))
    assert len(clock.slept) == 5  # control, run, control, run, control, run: 5 waits
    assert all(s == pytest.approx(SITE_SPACING_S) for s in clock.slept)


def test_a_failed_control_voids_that_sites_runs_in_the_batch_unstarted() -> None:
    tasks = [site("a1", "a.test"), site("b1", "b.test")]
    batches = iter([False] + [True] * 10)

    def control(host: str) -> bool:
        return next(batches) if host == "a.test" else True

    got, _, loads, _ = _run(
        tasks, lambda t, n: SiteRecord(record(t.stage.task_id, n), None), control=control
    )
    first = next(c for c in got if c.task_id == "a1")
    assert (first.verdict, first.reason) == ("void", "site_control_failed")
    assert ("run", "a1") in loads
    assert loads.count(("run", "a1")) == 3  # the voided batch's run was never started
    assert sum(c.task_id == "a1" and c.verdict == "pass" for c in got) == 3  # re-run later


def test_a_bot_wall_voids_the_site_for_the_rest_of_the_stage_with_no_retry() -> None:
    tasks = [site("a1", "a.test"), site("a2", "a.test"), site("b1", "b.test")]

    def attempt(t: SiteTask, n: int) -> SiteRecord:
        wall = "bot_wall" if t.stage.task_id == "a1" else None
        return SiteRecord(record(t.stage.task_id, n, final=None if wall else DONE), wall)

    got, _, loads, _ = _run(tasks, attempt)
    assert loads.count(("run", "a1")) == 1
    assert loads.count(("run", "a2")) == 0  # its site already walled: never run
    a2 = [c for c in got if c.task_id == "a2"]
    assert [(c.run, c.verdict, c.reason) for c in a2] == [(0, "void", "bot_wall")]
    assert loads.count(("control", "a.test")) == 1  # no control against a walled site
    assert sum(c.task_id == "b1" and c.verdict == "pass" for c in got) == 3


def test_voids_are_re_run_in_later_batches_within_the_bound() -> None:
    tasks = [site("a1", "a.test")]
    got, _, loads, _ = _run(
        tasks,
        lambda t, n: SiteRecord(record(t.stage.task_id, n, final=None), "site_down"),
    )
    assert loads.count(("run", "a1")) == 3 + MAX_RERUNS
    assert all(c.verdict == "void" for c in got)


def test_a_run_not_started_is_counted_and_re_run() -> None:
    tasks = [site("a1", "a.test")]
    answers = iter(["low_memory"] + [None] * 10)

    def attempt(t: SiteTask, n: int) -> SiteRecord | str:
        refused = next(answers)
        return refused if refused else SiteRecord(record(t.stage.task_id, n), None)

    got, not_started, _, _ = _run(tasks, attempt)
    assert not_started == 1
    assert sum(c.verdict == "pass" for c in got) == 3


# ---- the tasks from the committed table ---------------------------------------------------------


def _table(outcome: str = "done") -> dict[str, TableRow]:
    reason = "commit_not_in_step" if outcome == "blocked" else None
    return {
        t.task_id: TableRow(
            t.task_id,
            "0" * 64,
            FACTS,
            Expected(t.task_id, outcome, reason, "R1", "navigated", "why"),  # type: ignore[arg-type]
        )
        for t in real_site_tasks()
    }


def test_the_site_tasks_come_from_the_table_with_its_expected_outcome() -> None:
    tasks = site_tasks(_table("blocked"))
    assert len(tasks) == 30
    first = tasks[0].stage
    assert (first.fixture, first.expect, first.reason) == ("site", "blocked", "commit_not_in_step")
    assert all(t.host and t.stage.page.startswith("https://") for t in tasks)


def test_a_task_missing_from_the_table_is_refused() -> None:
    table = _table()
    table.pop(next(iter(table)))
    with pytest.raises(ValueError, match="unrated"):
        site_tasks(table)


def test_a_check_with_no_control_is_flagged_and_still_classified() -> None:
    """Rev 4.4a ruling (c): no control, classified normally, flagged `no_control`."""
    tasks = site_tasks(_table())
    flagged = [t.stage.task_id for t in tasks if not t.controlled]
    assert flagged  # the watch-page check names no fixed page
    summary = summarize(tasks, [], not_started=0, runs_per_task=3)
    assert summary.report["no_control"] == flagged


def test_the_summary_needs_24_valid_tasks_and_records_the_floor() -> None:
    tasks = site_tasks(_table())
    summary = summarize(tasks, [], not_started=0, runs_per_task=3)
    assert summary.gates["valid_tasks"] is True  # nothing site-voided yet
    assert summary.report["memory"]["floor_mb"] == 4096
    assert summary.report["set"] == "2b"


# ---- the parent never holds the session or the hooks ([V2.58] (3)) -------------------------------


def test_the_harness_never_takes_the_session_or_starts_a_monitor() -> None:
    source = Path(stage2.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        getattr(node.func, "attr", getattr(node.func, "id", ""))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & {
        "acquire_session",
        "start_monitor",
        "HookMonitor",
        "LiveWorld",
        "WindowsPlatform",
    }
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    banned = ("steerai.session", "steerai.loop2.live_world", "steerai.platform")
    assert not [m for m in modules if m.startswith(banned) or m.endswith("monitor")]


def test_each_batch_runs_in_the_interleaved_order() -> None:
    tasks = [site("a1", "a.test"), site("a2", "a.test"), site("b1", "b.test")]
    got, _, _, _ = _run(tasks, lambda t, n: SiteRecord(record(t.stage.task_id, n), None), runs=3)
    assert [c.task_id for c in got[:3]] == ["a1", "b1", "a2"]
