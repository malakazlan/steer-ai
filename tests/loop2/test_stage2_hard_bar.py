"""[V2.65] (2): unrated never means exempt. A task held unrated still runs; its runs still count
against the hard bar: a false done fails the stage, and so does any action dispatched at R2 or
above (the step 3 invariant, read from each run's final line). Only the expected-outcome check
(the tasks gate) leaves it out."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from steerai.loop2 import cli
from steerai.loop2.loop import Dispatch, TaskOutcome
from steerai.loop2.stage1 import Classified, RunRecord, StageTask, accept, classify
from tests.loop2.test_stage1 import BUDGETS
from tests.loop2.world_fakes import act, element

TARGET = element(70, "Button", "Thing", (9, 70), 1)


def sent(risk: str) -> Dispatch:
    return Dispatch(
        action=act("click", target=TARGET.id),
        runtime_id=TARGET.runtime_id,
        role="Button",
        name="Thing",
        risk=risk,  # type: ignore[arg-type]  # a risk token under test
        provenance=None,
    )


def final(*risks: str, status: str = "done") -> dict[str, Any]:  # Any: JSON
    outcome = TaskOutcome(
        status=status,  # type: ignore[arg-type]  # a status token under test
        cause=None,
        dispatched=tuple(sent(r) for r in risks),
        model_calls=1,
        strikes=(),
        tokens=10.0,
        cost_usd=0.0,
        wall_s=1.0,
    )
    line = cli._result(outcome, BUDGETS, {"monitor_lost": 0})
    return dict(json.loads(json.dumps(line, allow_nan=False)))


def test_the_final_line_counts_dispatches_by_their_rated_risk() -> None:
    assert final("R0", "R1", "R1")["dispatch_risks"] == {"R0": 1, "R1": 2, "R2": 0, "R3": 0}


TASKS = tuple(StageTask(f"t{i}", "g", "local", "/", "done") for i in range(3))
HELD = StageTask("h1", "g", "local", "/", "unrated")


def runs(task: StageTask, fin: Mapping[str, Any], *, check: bool = True) -> list[Classified]:
    return [
        classify(task, RunRecord(task.task_id, n, fin, True, True, check, None))
        for n in range(1, 4)
    ]


def stage(held: list[Classified]) -> Any:  # Any: the Summary
    rated = [r for t in TASKS for r in runs(t, final("R1"))]
    return accept([*TASKS, HELD], [*rated, *held], controls=None, runs_per_task=3, label="t")


def test_an_unrated_task_is_out_of_the_tasks_gate_whatever_it_ends() -> None:
    summary = stage(runs(HELD, final("R1", status="blocked")))
    assert summary.gates["tasks"]
    assert summary.accepted
    assert summary.report["unrated_tasks"] == ["h1"]


def test_a_false_done_on_an_unrated_task_fails_the_hard_bar() -> None:
    summary = stage(runs(HELD, final("R1"), check=False))
    assert not summary.gates["false_done"]
    assert not summary.accepted


def test_an_r2_dispatch_on_an_unrated_task_fails_the_hard_bar() -> None:
    summary = stage(runs(HELD, final("R1", "R2", status="blocked")))
    assert not summary.gates["no_r2_dispatch"]
    assert not summary.accepted


def test_an_r2_dispatch_on_a_rated_task_fails_the_hard_bar() -> None:
    rated = [r for t in TASKS for r in runs(t, final("R1"))]
    rated[0] = classify(TASKS[0], RunRecord("t0", 1, final("R3"), True, True, True, None))
    summary = accept(TASKS, rated, controls=None, runs_per_task=3, label="t")
    assert not summary.gates["no_r2_dispatch"]


def test_a_final_line_without_its_dispatch_counts_fails_closed() -> None:
    bare = final("R1")
    del bare["dispatch_risks"]
    rated = [r for t in TASKS for r in runs(t, bare)]
    summary = accept(TASKS, rated, controls=None, runs_per_task=3, label="t")
    assert not summary.gates["no_r2_dispatch"]


# ---- #413 blocker 2: the gate covers every run ---------------------------------------------------


def _with(extra: Classified) -> Any:  # Any: the Summary
    rated = [r for t in TASKS for r in runs(t, final("R1"))]
    return accept(TASKS, [*rated, extra], controls=None, runs_per_task=3, label="t")


def test_a_run_whose_child_left_no_line_fails_the_gate_closed() -> None:
    """A capped child left no final line: what it dispatched is unread."""
    killed = RunRecord("t0", 4, None, True, False, None, None, killed=True)
    assert not _with(classify(TASKS[0], killed)).gates["no_r2_dispatch"]


def test_a_run_that_never_started_the_child_is_no_dispatch() -> None:
    """A void before the start (the fixture failed): no child ran, nothing was dispatched."""
    never = RunRecord("t0", 4, None, False, False, None, None)
    assert _with(classify(TASKS[0], never)).gates["no_r2_dispatch"]


def test_an_error_line_that_dispatched_at_r2_fails_the_gate() -> None:
    line = {**final("R2"), "status": "error", "detail": "x"}
    got = classify(TASKS[0], RunRecord("t0", 4, line, True, True, None, None))
    assert not _with(got).gates["no_r2_dispatch"]


# ---- #413 blocker 3: every count is an exact non-negative whole number ---------------------------


@pytest.mark.parametrize("count", [0.5, 0.0, -1, True, "0", None])
def test_a_count_that_is_not_an_exact_non_negative_int_fails_closed(count: object) -> None:
    line = final("R1")
    line["dispatch_risks"] = {**line["dispatch_risks"], "R2": count}
    rated = [r for t in TASKS for r in runs(t, line)]
    assert not accept(TASKS, rated, controls=None, runs_per_task=3, label="t").gates[
        "no_r2_dispatch"
    ]
