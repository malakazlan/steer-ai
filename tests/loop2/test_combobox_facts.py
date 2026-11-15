"""[V2.64] Two: the "ComboBox miss" is reported as two facts per task, with no judgement: a
query-class ComboBox (searchbox AriaRole, or under a Search landmark) was listed to the model;
and any type targeted a ComboBox. On the outcome, the final line and the stage summary."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from steerai.loop2 import cli
from steerai.loop2.decision import Decision, SubgoalDoc
from steerai.loop2.loop import TaskOutcome
from steerai.loop2.runner import run_task
from steerai.loop2.stage1 import accept
from steerai.world.effects import Observed
from steerai.world.elements import Element
from tests.loop2.test_cli_v2 import Run
from tests.loop2.test_stage1 import DONE, _final, _ok
from tests.loop2.world_fakes import GO, PAGE, World, act, observed, task

COMBO = Element(id=7, depth=2, role="ComboBox", name="Search", box=(9, 70, 200, 90),
                runtime_id=(9, 7), parent_id=1)  # fmt: skip


def _screen(*, searchbox: bool) -> Observed:
    seen = observed([*PAGE, COMBO])
    roles = {COMBO.runtime_id: "searchbox"} if searchbox else {}
    return dataclasses.replace(seen, aria_roles=roles)


def _types_into_combo(*, searchbox: bool) -> World:
    subs = [SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="value")]
    world = World([Decision(subgoals=subs, actions=[act("type", target=COMBO.id, param="text")])])
    world.screen = _screen(searchbox=searchbox)
    return world


def test_a_query_class_combobox_listed_and_typed_into_sets_both_facts() -> None:
    outcome = run_task(task(), _types_into_combo(searchbox=True).ports())
    assert (outcome.query_combobox_listed, outcome.typed_into_combobox) == (True, True)


def test_a_plain_combobox_is_not_query_class() -> None:
    outcome = run_task(task(), _types_into_combo(searchbox=False).ports())
    assert (outcome.query_combobox_listed, outcome.typed_into_combobox) == (False, True)


def test_a_task_with_no_combobox_sets_neither() -> None:
    world = World([Decision(stop="cannot")])
    outcome = run_task(task(), world.ports())
    assert (outcome.query_combobox_listed, outcome.typed_into_combobox) == (False, False)


def test_a_click_on_a_combobox_is_not_a_type() -> None:
    subs = [SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="value")]
    world = World([Decision(subgoals=subs, actions=[act("click", target=GO.id)])])
    world.screen = _screen(searchbox=True)
    outcome = run_task(task(), world.ports())
    assert (outcome.query_combobox_listed, outcome.typed_into_combobox) == (True, False)


def test_the_final_line_carries_both_facts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = TaskOutcome(status="done", cause=None, dispatched=(), model_calls=1, strikes=(),
                          query_combobox_listed=True, typed_into_combobox=False)  # fmt: skip
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    run = Run(tmp_path)
    run()
    assert run.result()["combobox"] == {"query_class_listed": True, "typed_into": False}


def test_the_stage_summary_counts_runs_with_each_fact() -> None:
    finals = [
        _final(combobox={"query_class_listed": True, "typed_into": False}),
        _final(combobox={"query_class_listed": True, "typed_into": True}),
        _final(combobox={"query_class_listed": "x", "typed_into": False}),
    ]
    runs = [_ok(DONE, i + 1, final=f) for i, f in enumerate(finals)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    json.dumps(report, allow_nan=False)
    assert report["combobox"] == {"query_class_listed": 2, "typed_into": 1}
    assert report["run_facts_incomplete"] == 1
