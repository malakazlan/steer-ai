"""[V2.63] Q5: voids split. Infrastructure voids (memory, an unreadable log, a run not started)
above 1 in 10 make the stage inconclusive, as monitor losses above 1 in 10 do ([V2.59], Q3); site
voids (site changed, bot wall, control failed) are reported separately and are not in that
rate. A stage with a real-site minimum needs that many tasks valid to be conclusive. A bot wall
voids its site for the rest of the stage: no retry against it. In the shared core, so stage 2
reuses it."""

from __future__ import annotations

from steerai.loop2.stage1 import (
    SITE_VOIDS,
    Classified,
    RunRecord,
    StageTask,
    Summary,
    Verdict,
    accept,
    run_task_runs,
    site_voided,
)
from tests.loop2.test_stage1 import _final

TASKS = [StageTask(f"t{i}", "goal", "real", "/", "done") for i in range(30)]
Run = tuple[Verdict, str | None]
PASSES: tuple[Run, ...] = (("pass", None),) * 3


def _runs(task: StageTask, *verdicts: Run) -> list[Classified]:
    return [
        Classified(task.task_id, i + 1, v, reason, v == "monitor_lost", _final())
        for i, (v, reason) in enumerate(verdicts)
    ]


def _stage(
    extra: dict[str, list[Classified]] | None = None, min_valid_tasks: int | None = None
) -> Summary:
    runs = [r for t in TASKS for r in (extra or {}).get(t.task_id, _runs(t, *PASSES))]
    return accept(TASKS, runs, controls=None, runs_per_task=3, label="2b",
                  min_valid_tasks=min_valid_tasks)  # fmt: skip


def test_the_site_voids_are_closed() -> None:
    assert frozenset({"site_changed", "bot_wall", "site_control_failed"}) == SITE_VOIDS


def test_site_voids_are_reported_apart_and_not_in_the_void_rate() -> None:
    """12 tasks each lost one run to a failed control and were re-run to 3 counted runs."""
    extra = {t.task_id: _runs(t, ("void", "site_control_failed"), *PASSES) for t in TASKS[:12]}
    summary = _stage(extra, min_valid_tasks=24)
    assert summary.gates["void"] is True
    assert summary.report["site_voids"] == {"site_control_failed": 12}
    assert summary.report["void_rate"] == 0.0
    assert summary.conclusive is True


def test_infrastructure_voids_over_one_in_ten_make_the_stage_inconclusive_not_failed() -> None:
    extra = {t.task_id: _runs(t, ("void", "log_unreadable"), *PASSES) for t in TASKS[:11]}
    summary = _stage(extra)
    assert summary.gates["void"] is False
    assert summary.conclusive is False
    assert summary.report["result"] == "inconclusive"


def test_monitor_losses_over_one_in_ten_make_it_inconclusive_too() -> None:
    extra = {t.task_id: _runs(t, ("monitor_lost", "monitor_lost"), *PASSES) for t in TASKS[:11]}
    assert _stage(extra).report["result"] == "inconclusive"


def test_fewer_than_the_minimum_valid_tasks_is_inconclusive() -> None:
    """A task short of its counted runs because of a site void is not valid (unmeasured), and
    is not a failed task either."""
    walled = {t.task_id: _runs(t, ("void", "bot_wall")) for t in TASKS[:7]}
    summary = _stage(walled, min_valid_tasks=24)
    assert summary.report["valid_tasks"] == 23
    assert summary.report["site_invalid_tasks"] == [t.task_id for t in TASKS[:7]]
    assert summary.gates["tasks"] is True  # judged on the valid tasks
    assert summary.report["result"] == "inconclusive"


def test_twenty_four_valid_of_thirty_is_conclusive_and_can_be_accepted() -> None:
    walled = {t.task_id: _runs(t, ("void", "bot_wall")) for t in TASKS[:6]}
    summary = _stage(walled, min_valid_tasks=24)
    assert summary.report["valid_tasks"] == 24
    assert (summary.conclusive, summary.accepted) == (True, True)
    assert summary.report["result"] == "accepted"


def test_a_conclusive_stage_with_a_failed_task_is_rejected() -> None:
    failed = {TASKS[0].task_id: _runs(TASKS[0], ("fail", "no_progress"), *PASSES[:2])}
    summary = _stage(failed, min_valid_tasks=24)
    assert (summary.conclusive, summary.accepted) == (True, False)
    assert summary.report["result"] == "rejected"


def test_a_task_short_for_its_own_reasons_still_fails_the_tasks_gate() -> None:
    """Only a site void makes a short task unmeasured; an infrastructure void does not."""
    short = {TASKS[0].task_id: _runs(TASKS[0], ("void", "log_unreadable"), *PASSES[:2])}
    summary = _stage(short, min_valid_tasks=24)
    assert summary.gates["tasks"] is False
    assert TASKS[0].task_id not in summary.report["site_invalid_tasks"]


def test_stage_1_has_no_minimum_and_keeps_its_gates() -> None:
    summary = _stage()
    assert "valid_tasks" not in summary.gates
    assert summary.report["result"] == "accepted"


# ---- a bot wall: no retry against it, and the site is void for the rest of the stage -------------


def test_a_bot_wall_is_never_retried() -> None:
    task = TASKS[0]
    asked: list[int] = []

    def attempt(run: int) -> RunRecord:
        asked.append(run)
        return RunRecord(task.task_id, run, None, False, False, None, None)

    def walled(record: RunRecord) -> Classified:
        return Classified(task.task_id, record.run, "void", "bot_wall", False, None)

    runs, _ = run_task_runs(task, runs=3, attempt=attempt, classifier=walled)
    assert asked == [1]
    assert [r.reason for r in runs] == ["bot_wall"]


def test_a_walled_sites_other_tasks_are_void_without_a_run() -> None:
    task = TASKS[1]
    skipped = site_voided(task)
    assert (skipped.verdict, skipped.reason, skipped.run) == ("void", "bot_wall", 0)
    summary = _stage({task.task_id: [skipped]}, min_valid_tasks=24)
    assert task.task_id in summary.report["site_invalid_tasks"]


# ---- #393 review: a scored failure is never hidden behind a site void ----------------------------


def test_a_task_with_a_scored_fail_stays_measured_whatever_voids_follow() -> None:
    runs = _runs(TASKS[0], ("fail", "no_progress"), ("void", "site_control_failed"),
                 ("void", "bot_wall"))  # fmt: skip
    hidden = {TASKS[0].task_id: runs}
    summary = _stage(hidden, min_valid_tasks=24)
    assert TASKS[0].task_id not in summary.report["site_invalid_tasks"]
    assert summary.gates["tasks"] is False
    assert summary.report["result"] == "rejected"


def test_a_false_done_then_a_bot_wall_stays_measured() -> None:
    hidden = {TASKS[0].task_id: _runs(TASKS[0], ("false_done", None), ("void", "bot_wall"))}
    summary = _stage(hidden, min_valid_tasks=24)
    assert TASKS[0].task_id not in summary.report["site_invalid_tasks"]
    assert summary.gates["false_done"] is False


def test_each_site_void_reason_has_its_rate_reported_never_gated() -> None:
    """A classifier that over-assigns site voids shows here."""
    extra = {t.task_id: _runs(t, ("void", "site_changed"), *PASSES) for t in TASKS[:10]}
    summary = _stage(extra, min_valid_tasks=24)
    assert summary.report["site_void_rates"] == {"site_changed": round(10 / 100, 3)}
    assert "site_void_rates" not in summary.gates
    assert summary.report["result"] == "accepted"


def test_a_void_breach_outranks_a_failed_gate() -> None:
    extra = {t.task_id: _runs(t, ("void", "log_unreadable"), *PASSES) for t in TASKS[:11]}
    extra[TASKS[29].task_id] = _runs(TASKS[29], ("fail", "no_progress"), *PASSES[:2])
    summary = _stage(extra, min_valid_tasks=24)
    assert (summary.gates["void"], summary.gates["tasks"]) == (False, False)
    assert summary.report["result"] == "inconclusive"
