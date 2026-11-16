"""[V2.63], the counts table: every count the rulings asked for is collected before stage 2. The
counts already on `TaskOutcome` but not on the final line join it in one `counts` block, with the
submit rule's firings ([V2.25]) counted per parent kind; the stage summary sums them."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2 import cli
from steerai.loop2.loop import PARENT_KINDS, RiskVerdict, TaskOutcome, action_risk
from steerai.loop2.runner import run_task
from steerai.loop2.stage1 import accept
from tests.loop2.test_cli_v2 import Run
from tests.loop2.test_stage1 import DONE, _final, _ok
from tests.loop2.world_fakes import GO, World, act, decision, task

OUTCOME = TaskOutcome(
    status="done",
    cause=None,
    dispatched=(),
    model_calls=1,
    strikes=(),
    second_toggles=1,
    second_toggles_refused=2,
    toggles_settled_on_reread=3,
    fields_not_empty=4,
    not_own_text=5,
    evidence_taken=11,
    target_changes=6,
    focus_losses=7,
    history_dropped=8,
    turn_trace_dropped=10,
    turn_trace_errors=11,
    pre_task_draft_clicks=9,
    audit_calls=3,
    audit_allowed=1,
    audit_vetoed=1,
    audit_failed=1,
    audit_no_call=1,
    audit_budget_stops=1,
    audit_tokens=55,
    intake_checks=3,
    intake_passed=1,
    intake_rejected=1,
    intake_failed=1,
    intake_no_call=1,
    intake_memo_refused=1,
    intake_budget_stops=1,
    intake_tokens=66,
    saved_calls=10,
    submit_parents=(("document", 1), ("main", 0), ("narrower", 2)),
)
COUNTS = {
    "second_toggles": 1,
    "second_toggles_refused": 2,
    "toggles_settled_on_reread": 3,
    "fields_not_empty": 4,
    "not_own_text": 5,
    "evidence_taken": 11,
    "target_changes": 6,
    "focus_losses": 7,
    "history_dropped": 8,
    "turn_trace_dropped": 10,
    "turn_trace_errors": 11,
    "pre_task_draft_clicks": 9,
    "audit_calls": 3,
    "audit_allowed": 1,
    "audit_vetoed": 1,
    "audit_failed": 1,
    "audit_no_call": 1,
    "audit_budget_stops": 1,
    "audit_tokens": 55,
    "intake_checks": 3,
    "intake_passed": 1,
    "intake_rejected": 1,
    "intake_failed": 1,
    "intake_no_call": 1,
    "intake_memo_refused": 1,
    "intake_budget_stops": 1,
    "intake_tokens": 66,
    "saved_calls": 10,
    "submit_parents": {"document": 1, "main": 0, "narrower": 2},
}


def test_the_parent_kinds_are_closed() -> None:
    assert PARENT_KINDS == ("document", "main", "narrower")


def test_the_submit_rule_firings_are_counted_per_parent_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every action the risk path rates once; a firing is counted where it is rated."""
    real = action_risk

    def fired(*args: Any, **kwargs: Any) -> RiskVerdict:  # Any: action_risk's own arguments
        return dataclasses.replace(real(*args, **kwargs), submit_parent="narrower")

    monkeypatch.setattr("steerai.loop2.runner.action_risk", fired)
    world = World([decision(act("click", target=GO.id), first=True)])
    outcome = run_task(task(), world.ports())
    assert dict(outcome.submit_parents) == {"document": 0, "main": 0, "narrower": 1}


def test_no_firing_counts_nothing() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    assert sum(n for _, n in run_task(task(), world.ports()).submit_parents) == 0


def _line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: TaskOutcome) -> Any:  # JSON
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    run = Run(tmp_path)
    run()
    return run.result()


def test_the_final_line_carries_the_outcome_only_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _line(monkeypatch, tmp_path, OUTCOME)["counts"] == COUNTS


def test_the_unfinished_line_says_the_counts_are_unknown() -> None:
    from steerai.loop2.task import Budgets  # noqa: PLC0415

    shape = cli._unfinished(Budgets(max_actions=1, max_model_calls=1, max_tokens=1,
                                    max_cost_usd=1.0, max_wall_s=1.0))  # fmt: skip
    assert shape["counts"] is None


def test_the_stage_summary_sums_the_counts_and_says_odd_ones() -> None:
    odd = {**COUNTS, "focus_losses": "x", "submit_parents": {"document": 1, "elsewhere": 9}}
    finals = [_final(counts=COUNTS), _final(counts=COUNTS), _final(counts=odd)]
    runs = [_ok(DONE, i + 1, final=f) for i, f in enumerate(finals)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    json.dumps(report, allow_nan=False)
    assert report["counts"]["history_dropped"] == 24
    assert report["counts"]["turn_trace_dropped"] == 30
    assert report["counts"]["turn_trace_errors"] == 33  # #463 (1)
    assert report["counts"]["focus_losses"] == 14  # the odd run's is said, not summed
    assert report["counts"]["submit_parents"] == {"document": 3, "main": 0, "narrower": 4}
    assert report["run_facts_incomplete"] == 2  # "x" and "elsewhere"
