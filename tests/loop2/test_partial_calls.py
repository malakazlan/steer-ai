"""[V2.63] (e), [V2.35]: a model call may be spent on a partial page (one still growing, or a
Document younger than the load guard), counted. Counted where every call is counted, from the
observation the call was made on; on the outcome, the final line and the stage summary."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from steerai.loop2 import cli
from steerai.loop2.loop import TaskOutcome
from steerai.loop2.runner import run_task
from steerai.loop2.stage1 import accept
from tests.loop2.test_cli_v2 import Run
from tests.loop2.test_stage1 import DONE, _final, _ok
from tests.loop2.world_fakes import World, act, decision, observed, task


def test_calls_on_a_growing_page_are_counted() -> None:
    world = World([decision(act("wait"), first=True), *(decision(act("wait")) for _ in range(4))])
    world.screen = dataclasses.replace(observed(), growing=True, document_since=0.0)
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls >= 1
    assert outcome.partial_page_calls == outcome.model_calls


def test_calls_on_a_settled_page_are_not() -> None:
    world = World([decision(act("wait"), first=True)])
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls >= 1
    assert outcome.partial_page_calls == 0


def test_the_final_line_carries_the_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = TaskOutcome(status="done", cause=None, dispatched=(), model_calls=3, strikes=(),
                          partial_page_calls=2)  # fmt: skip
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    run = Run(tmp_path)
    run()
    assert run.result()["partial_page_calls"] == 2


def test_the_unfinished_line_says_it_is_unknown() -> None:
    from steerai.loop2.task import Budgets  # noqa: PLC0415

    shape = cli._unfinished(Budgets(max_actions=1, max_model_calls=1, max_tokens=1,
                                    max_cost_usd=1.0, max_wall_s=1.0))  # fmt: skip
    assert shape["partial_page_calls"] is None


def test_the_stage_summary_sums_it_and_says_an_odd_one() -> None:
    finals = [_final(partial_page_calls=2), _final(partial_page_calls=1),
              _final(partial_page_calls="x")]  # fmt: skip
    runs = [_ok(DONE, i + 1, final=f) for i, f in enumerate(finals)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    json.dumps(report, allow_nan=False)
    assert report["partial_page_calls"] == 3
    assert report["run_facts_incomplete"] == 1
