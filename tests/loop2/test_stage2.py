"""Stage 2's harness, the pure parts (stage 2 plan rev 4.4 §2, [V2.63]): the start-page check's
void rules and their order, the classification in front of stage 1's, the round robin over sites
with the per-host spacing on a fake clock, the per-site controls each batch, the bot wall that
voids a site for the rest of the stage, and the summary's 24-of-30 and 4096 MB floor."""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2 import stage2
from steerai.loop2.stage1 import FLOOR_MB, MAX_RERUNS, SITE_VOIDS, RunRecord, StageTask
from steerai.loop2.stage1_expected import Expected
from steerai.loop2.stage2 import (
    MIN_VALID_TASKS,
    ControlResult,
    SiteRecord,
    SiteTask,
    StartPage,
    classify_site,
    harness_failed,
    interleaved,
    invalid_run_void,
    needs_confirmation,
    run_stage,
    site_tasks,
    start_void,
    summarize,
    volume_bound,
)
from steerai.loop2.stage2_expected import (
    DAILY_CAP,
    SITE_SPACING_S,
    DecidingFacts,
    SiteRow,
    Spacer,
    TableRow,
    real_site_tasks,
)

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
        site("t1", "a.test"), SiteRecord(record("t1", 1, final=None), "site_changed")
    )
    assert (got.verdict, got.reason) == ("void", "site_changed")


def test_with_no_site_void_stage_1s_rules_decide() -> None:
    got = classify_site(site("t1", "a.test"), SiteRecord(record("t1", 1), None))
    assert got.verdict == "pass"
    false = RunRecord("t1", 1, DONE, True, True, False, None)
    assert classify_site(site("t1", "a.test"), SiteRecord(false, None)).verdict == "false_done"


# ---- #403 blocker 1: a wall after the run is the site's only when confirmed ---------------------


def walled(final: Mapping[str, Any] | None = DONE, *, killed: bool = False) -> RunRecord:
    """The after-read showed a wall: the log is unread."""
    return RunRecord("t1", 1, final, True, False, None, None, killed=killed)


def test_a_confirmed_wall_on_the_tasks_own_host_is_a_bot_wall() -> None:
    got = classify_site(
        site("t1", "a.test"), SiteRecord(walled(), None, wall_host="a.test"), confirmed=True
    )
    assert (got.verdict, got.reason) == ("void", "bot_wall")


def test_a_wall_on_another_host_is_scored() -> None:
    """Steer navigated elsewhere and met a wall there: Steer's result, never the site's."""
    got = classify_site(
        site("t1", "a.test"), SiteRecord(walled(), None, wall_host="b.test"), confirmed=True
    )
    assert got.verdict == "fail"


def test_an_unconfirmed_wall_with_done_is_a_fail() -> None:
    got = classify_site(
        site("t1", "a.test"), SiteRecord(walled(), None, wall_host="a.test"), confirmed=False
    )
    assert (got.verdict, got.reason) == ("fail", "done_log_unread")


def test_a_cap_kill_with_a_wall_is_a_fail() -> None:
    """Stage 1's Steer-caused results come first, whatever the after-read shows."""
    got = classify_site(
        site("t1", "a.test"),
        SiteRecord(walled(None, killed=True), None, wall_host="a.test"),
        confirmed=True,
    )
    assert (got.verdict, got.reason) == ("fail", "cap_killed")


@pytest.mark.parametrize(
    "final",
    [
        {"status": "nonsense"},  # no_final_line
        {"status": "done", "reason": "internal_error", "used": {"max_actions": 1}},  # internal
        {"status": "error", "detail": "x"},  # start_refused
    ],
)
def test_every_steer_caused_result_wins_over_a_confirmed_wall(final: Mapping[str, Any]) -> None:
    got = classify_site(
        site("t1", "a.test"), SiteRecord(walled(final), None, wall_host="a.test"), confirmed=True
    )
    assert got.verdict == "fail"


def test_the_wall_is_confirmed_only_when_it_would_count() -> None:
    assert needs_confirmation(site("t1", "a.test"), SiteRecord(walled(), None, wall_host="a.test"))
    assert not needs_confirmation(
        site("t1", "a.test"), SiteRecord(walled(), None, wall_host="b.test")
    )
    assert not needs_confirmation(
        site("t1", "a.test"),
        SiteRecord(walled(None, killed=True), None, wall_host="a.test"),
    )
    assert not needs_confirmation(site("t1", "a.test"), SiteRecord(record("t1", 1), None))


# ---- #403 blocker 2: the void of a run that never started, by the run's own token -------------


@pytest.mark.parametrize(
    ("token", "void"),
    [
        ("page_not_loaded", "site_down"),
        ("bot_check", "bot_wall"),
        ("result_link_preexisting", "start_page_differs"),
        ("page_targets:2", None),
        ("anything_else", None),
    ],
)
def test_an_invalid_run_maps_to_a_site_void_by_its_token_only(token: str, void: str | None) -> None:
    assert invalid_run_void(token) == void


def test_a_harness_failure_before_the_page_read_is_an_infrastructure_void() -> None:
    """Launch, window, DevTools or UIA failing is the machine's, never `site_down`."""
    got = classify_site(site("t1", "a.test"), SiteRecord(harness_failed("t1", 1), None))
    assert (got.verdict, got.reason) == ("void", "fixture_failed_before_start")
    assert got.reason not in SITE_VOIDS


def test_an_unread_start_page_is_an_infrastructure_void() -> None:
    """#401 note: an unread AriaRole or landmark map is unread, never `site_changed`."""
    unread = dataclasses.replace(FACTS, unread=("aria_role",))
    void = start_void(StartPage(loaded=True, wall=False, facts=unread), FACTS)
    assert void == "start_page_unread"
    assert void not in SITE_VOIDS


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
    control: Any = lambda _host: "pass",  # Any: the test's control callable
    confirm: Any = lambda _task: False,  # Any: the test's wall confirmation
) -> tuple[list[Any], int, list[tuple[str, str]], Clock]:
    clock = Clock()
    loads: list[tuple[str, str]] = []

    def logged_attempt(t: SiteTask, n: int) -> Any:
        loads.append(("run", t.stage.task_id))
        return attempt(t, n)

    def logged_control(host: str) -> ControlResult:
        loads.append(("control", host))
        result: ControlResult = control(host)
        return result

    def logged_confirm(t: SiteTask) -> bool:
        loads.append(("confirm", t.host))
        return bool(confirm(t))

    got, not_started = run_stage(
        tasks,
        runs=runs,
        attempt=logged_attempt,
        control=logged_control,
        confirm=logged_confirm,
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
    _, _, loads, clock = _run(tasks, lambda t, n: SiteRecord(record(t.stage.task_id, n), None))
    assert loads == [("control", "a.test")] + [("run", "a1")] * 3
    assert len(clock.slept) == 3  # control, then three runs: every load after the first waited
    assert all(s == pytest.approx(SITE_SPACING_S) for s in clock.slept)


def test_a_failed_control_voids_that_sites_runs_in_the_batch_unstarted() -> None:
    tasks = [site("a1", "a.test"), site("b1", "b.test")]
    batches = iter(["fail"] + ["pass"] * 10)

    def control(host: str) -> str:
        return next(batches) if host == "a.test" else "pass"

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

    clock = Clock()
    spacer = Spacer(clock, clock.sleep)
    got, not_started = run_stage(
        tasks,
        runs=3,
        attempt=attempt,
        control=lambda _h: "pass",
        confirm=lambda _t: False,
        spacer=spacer,
    )
    assert not_started == 1
    assert sum(c.verdict == "pass" for c in got) == 3
    # [V2.65] (3): one control, in the first batch; the run never started loaded nothing
    assert spacer.loads == {"a.test": 1 + 3}


# ---- the tasks from the committed table ---------------------------------------------------------


def _table(outcome: str = "done") -> dict[str, TableRow]:
    reason = "commit_not_in_step" if outcome == "blocked" else None
    return {
        t.task_id: TableRow(
            t.task_id,
            "0" * 64,
            FACTS,
            # the test passes each outcome as a plain str; Expected wants the Outcome literal
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


# ---- #403 blockers 3-6, and the notes -----------------------------------------------------------


def test_a_task_with_no_acts_row_is_refused_when_acts_are_given() -> None:
    table = _table()
    acts = {t.task_id: SiteRow((), None, "why") for t in real_site_tasks()}
    acts.pop(next(iter(acts)))
    with pytest.raises(ValueError, match="no_acts"):
        site_tasks(table, acts)


def test_a_walled_task_that_already_scored_is_site_voided() -> None:
    """a2 passed run 1; a1 then walls the host in batch 2: a2 is short of its runs and must be
    unmeasured, never failed for runs it could not have."""
    tasks = [site("a1", "a.test"), site("a2", "a.test")]

    def attempt(t: SiteTask, n: int) -> SiteRecord:
        if t.stage.task_id == "a1" and n == 2:
            return SiteRecord(record("a1", n, final=None), "bot_wall")
        return SiteRecord(record(t.stage.task_id, n), None)

    got, _, _, _ = _run(tasks, attempt)
    a2 = [(c.run, c.verdict, c.reason) for c in got if c.task_id == "a2"]
    assert a2 == [(1, "pass", None), (0, "void", "bot_wall")]
    summary = summarize(
        [*tasks, *[site(f"b{i}", f"b{i}.test") for i in range(MIN_VALID_TASKS)]],
        got,
        not_started=0,
        runs_per_task=3,
    )
    assert "a2" in summary.report["site_invalid_tasks"]


def test_a_wall_on_the_control_walls_the_host_with_no_retry() -> None:
    tasks = [site("a1", "a.test"), site("b1", "b.test")]
    got, _, loads, _ = _run(
        tasks,
        lambda t, n: SiteRecord(record(t.stage.task_id, n), None),
        control=lambda host: "wall" if host == "a.test" else "pass",
    )
    assert loads.count(("control", "a.test")) == 1
    assert ("run", "a1") not in loads
    assert [(c.run, c.verdict, c.reason) for c in got if c.task_id == "a1"] == [
        (0, "void", "bot_wall")
    ]


def test_a_wall_after_the_run_is_confirmed_by_a_spaced_harness_load() -> None:
    tasks = [site("a1", "a.test")]

    def attempt(t: SiteTask, n: int) -> SiteRecord:
        return SiteRecord(
            RunRecord("a1", n, DONE, True, False, None, None), None, wall_host="a.test"
        )

    got, _, loads, clock = _run(tasks, attempt, confirm=lambda _t: True)
    assert loads == [("control", "a.test"), ("run", "a1"), ("confirm", "a.test")]
    assert [(c.verdict, c.reason) for c in got] == [("void", "bot_wall")]
    assert len(clock.slept) == 2  # the run and the confirmation each waited out the spacing


def test_a_control_runs_only_for_a_host_with_a_task_in_the_batch() -> None:
    tasks = [site("a1", "a.test"), site("b1", "b.test")]

    def attempt(t: SiteTask, n: int) -> SiteRecord:
        down = t.stage.task_id == "a1" and n > 1
        return SiteRecord(record(t.stage.task_id, n, final=None if down else DONE),
                          "site_down" if down else None)  # fmt: skip

    _, _, loads, _ = _run(tasks, attempt, runs=1)
    assert loads.count(("control", "b.test")) == 1  # b1 scored in batch 1: no more controls


def test_re_runs_are_spaced_too() -> None:
    tasks = [site("a1", "a.test")]
    _, _, _, clock = _run(
        tasks, lambda _t, n: SiteRecord(record("a1", n, final=None), "site_down"), runs=1
    )
    # 1 + MAX_RERUNS batches of control then run: every load after the first waited
    assert len(clock.slept) == 2 * (1 + MAX_RERUNS) - 1
    assert all(s == pytest.approx(SITE_SPACING_S) for s in clock.slept)


def test_the_loads_per_host_are_counted_and_reported() -> None:
    tasks = [site("a1", "a.test")]
    clock = Clock()
    spacer = Spacer(clock, clock.sleep)
    got, not_started = run_stage(
        tasks,
        runs=3,
        attempt=lambda _t, n: SiteRecord(record("a1", n), None),
        control=lambda _h: "pass",
        confirm=lambda _t: False,
        spacer=spacer,
    )
    summary = summarize(tasks, got, not_started=not_started, runs_per_task=3, loads=spacer.loads)
    assert summary.report["harness_loads_per_host"] == {"a.test": 4}
    assert summary.report["harness_loads_per_host_max"] == 4
    # [V2.65] (3): 3 runs and 3 re-runs, one control first and one per re-run batch at most
    assert summary.report["harness_loads_bound"] == volume_bound(tasks, runs=3) == {"a.test": 10}
    assert summary.report["harness_days_bound"] == {"a.test": 1}


def test_the_volume_bound_per_host_from_the_task_data() -> None:
    """Every run, re-run and control a host can take, before any wall confirmation: what the
    policy's "about 8 loads per host" comes to on the pooled set."""
    bound = volume_bound(site_tasks(_table()), runs=3)
    assert set(bound) == {t.host for t in site_tasks(_table())}
    assert all(n >= 3 + MAX_RERUNS for n in bound.values())


# ---- [V2.65] (3): a control once per host per session; at most 12 loads a host a day ----------


class Days:
    """The calendar day on the fake clock: a day is 86,400 s."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def __call__(self) -> int:
        return int(self.clock() // 86_400)


def _staged(tasks: list[SiteTask], attempt: Any, *, control: Any = lambda _h: "pass") -> Any:
    clock = Clock()
    loads: list[tuple[int, str, str]] = []
    days = Days(clock)
    spacer = Spacer(clock, clock.sleep, day=days)

    def logged(t: SiteTask, n: int) -> Any:
        loads.append((days(), "run", t.host))
        return attempt(t, n)

    def ctl(host: str) -> Any:
        loads.append((days(), "control", host))
        return control(host)

    got, _ = run_stage(
        tasks, runs=3, attempt=logged, control=ctl, confirm=lambda _t: False, spacer=spacer
    )
    return got, loads, clock, spacer


def test_a_control_repeats_after_a_site_void_on_its_host() -> None:
    tasks = [site("a1", "a.test")]
    outcomes = iter(["site_down", None, None, None])

    def attempt(t: SiteTask, n: int) -> SiteRecord:
        void = next(outcomes)
        return SiteRecord(record("a1", n, final=None if void else DONE), void)

    _, loads, _, _ = _staged(tasks, attempt)
    kinds = [kind for _, kind, _ in loads]
    assert kinds == ["control", "run", "control", "run", "run", "run"]


def test_a_control_repeats_for_a_re_run_on_a_later_day() -> None:
    """A re-run batch on a new day controls again; a later batch on the same day does not."""
    tasks = [site("a1", "a.test")]
    clock_ref: list[Clock] = []
    outcomes = iter(["monitor_lost", None, None, None])

    def attempt(t: SiteTask, n: int) -> SiteRecord:
        lost = next(outcomes) == "monitor_lost"
        if lost:
            clock_ref[0].now += 86_400.0  # the next batch falls on the next day
        final = {**DONE, "monitor_lost": True} if lost else DONE
        return SiteRecord(record("a1", n, final=final), None)

    clock = Clock()
    clock_ref.append(clock)
    days = Days(clock)
    loads: list[tuple[int, str]] = []

    def logged(t: SiteTask, n: int) -> SiteRecord:
        loads.append((days(), "run"))
        return attempt(t, n)

    def ctl(_host: str) -> ControlResult:
        loads.append((days(), "control"))
        return "pass"

    run_stage(tasks, runs=3, attempt=logged, control=ctl, confirm=lambda _t: False,
              spacer=Spacer(clock, clock.sleep, day=days))  # fmt: skip
    assert [k for _, k in loads] == ["control", "run", "control", "run", "run", "run"]


def test_a_host_stops_at_its_daily_cap_and_resumes_the_next_day() -> None:
    tasks = [site(f"a{i}", "a.test") for i in range(5)]  # 15 runs and a control on one host
    _, loads, _, spacer = _staged(tasks, lambda t, n: SiteRecord(record(t.stage.task_id, n), None))
    per_day: dict[int, int] = {}
    for day, _, _ in loads:
        per_day[day] = per_day.get(day, 0) + 1
    assert all(n <= DAILY_CAP for n in per_day.values())
    assert len(per_day) == 2  # 16 loads at 12 a day: two days
    assert sum(per_day.values()) == 16 == spacer.loads["a.test"]


def test_the_policy_caps() -> None:
    assert DAILY_CAP == 12


def test_an_unrated_task_is_not_run_and_not_counted() -> None:
    """The coordinator's ruling (2026-11-15): out of the tables and out of acceptance until the
    lead rules, and listed with its reason."""
    table = _table()
    gone = next(iter(table))
    table.pop(gone)
    tasks = site_tasks(table, unrated={gone: "target_after_opener"})
    assert gone not in {t.stage.task_id for t in tasks}
    summary = summarize(
        tasks,
        [],
        not_started=0,
        runs_per_task=3,
        unrated={gone: "target_after_opener"},
    )
    assert summary.report["unrated"] == {gone: "target_after_opener"}
