"""[V2.63] (e), [V2.46]: the repeat rule's count. Every refusal is counted by its closed token
(the repeat rule is `repeat_send`), on the outcome, the final line and the stage summary; the
strike for the same action with the same effect is already counted (`strikes.by_reason`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2 import cli
from steerai.loop2.loop import TaskOutcome
from steerai.loop2.runner import run_task
from steerai.loop2.stage1 import accept
from tests.loop2.test_cli_v2 import Run
from tests.loop2.test_rt_deferred import _mixed
from tests.loop2.test_stage1 import DONE, _final, _ok
from tests.loop2.world_fakes import GO, World, act, decision, task


def test_a_repeat_send_refusal_is_counted_by_its_token() -> None:
    world = _mixed(decision(act("click", target=GO.id)))
    outcome = run_task(task(), world.ports())
    assert dict(outcome.refusals).get("repeat_send", 0) >= 1


def test_a_run_with_no_refusal_counts_none() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    assert run_task(task(), world.ports()).refusals == ()


def test_the_final_line_carries_the_refusals_by_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcome = TaskOutcome(status="done", cause=None, dispatched=(), model_calls=1, strikes=(),
                          refusals=(("repeat_send", 2), ("id_not_listed", 1)))  # fmt: skip
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    run = Run(tmp_path)
    run()
    assert run.result()["refusals"] == {"repeat_send": 2, "id_not_listed": 1}


def test_the_unfinished_line_says_the_refusals_are_unknown() -> None:
    from steerai.loop2.task import Budgets  # noqa: PLC0415

    shape = cli._unfinished(Budgets(max_actions=1, max_model_calls=1, max_tokens=1,
                                    max_cost_usd=1.0, max_wall_s=1.0))  # fmt: skip
    assert shape["refusals"] is None


def test_the_stage_summary_sums_refusals_by_token_and_says_odd_ones() -> None:
    finals: list[dict[str, Any]] = [
        _final(refusals={"repeat_send": 2}),
        _final(refusals={"repeat_send": 1, "id_not_listed": 1}),
        _final(refusals={"made_up": 4, "repeat_send": "x"}),
    ]
    runs = [_ok(DONE, i + 1, final=f) for i, f in enumerate(finals)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    json.dumps(report, allow_nan=False)
    assert report["refusals"] == {"repeat_send": 3, "id_not_listed": 1}
    assert report["run_facts_incomplete"] == 2
