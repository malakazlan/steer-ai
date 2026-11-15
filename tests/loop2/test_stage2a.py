"""Stage 2's group 2a (stage 2 plan rev 4.4, [V2.63] Q1): the pooled set's local tasks, with the
same harness and gates as 2b and no network. The native tasks that need no dialog and no second
window are in; the rest wait for step 7 and are listed, not run (data, never a rule naming an
app in code)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from steerai.loop2 import stage2a
from steerai.loop2.stage1 import FLOOR_MB
from steerai.loop2.stage1_expected import Expected
from steerai.loop2.stage2_expected import DecidingFacts, TableRow, table_from_reads
from steerai.loop2.stage2a import (
    LABEL,
    OUT_OF_STEP,
    load_out_of_step,
    local_stage_tasks,
    local_tasks,
    read_groups,
    start_key,
    summarize,
)
from steerai.probes.task_suite import H2, H3, H4, H5, H6, HELD_OUT, TASKS

FACTS = DecidingFacts("Edit", None, False, None, None, "R1")


def _pooled_local() -> list[str]:
    return [
        t.task_id
        for s in (TASKS, HELD_OUT, H2, H3, H4, H5, H6)
        for t in s
        if not t.start.startswith("https://")
    ]


def test_group_2a_is_the_pooled_local_tasks_less_those_out_of_step() -> None:
    pooled = list(dict.fromkeys(_pooled_local()))
    assert len(pooled) == 40
    ids = [t.task_id for t in local_tasks()]
    assert ids == [i for i in pooled if i not in OUT_OF_STEP]
    assert len(ids) == 40 - len(OUT_OF_STEP)
    assert not any(t.start.startswith("https://") for t in local_tasks())


def test_the_tasks_out_of_step_are_data_each_with_its_reason() -> None:
    assert load_out_of_step(stage2a.OUT_OF_STEP_FILE.read_text(encoding="utf-8")) == OUT_OF_STEP
    assert OUT_OF_STEP  # the save-dialog task waits for step 7
    assert all(reason.startswith("step 7:") for reason in OUT_OF_STEP.values())
    assert set(OUT_OF_STEP) <= set(_pooled_local())


@pytest.mark.parametrize(
    "bad", ['["save1"]', '{"save1": ""}', '{"save1": 7}', '{"zz9": "step 7: x"}']
)
def test_an_out_of_step_file_that_is_not_valid_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="out of step"):
        load_out_of_step(bad)


def _table(outcome: str = "done") -> dict[str, TableRow]:
    reason = "commit_not_in_step" if outcome == "blocked" else None
    return {
        t.task_id: TableRow(
            t.task_id,
            "0" * 64,
            FACTS,
            Expected(t.task_id, outcome, reason, "R1", "state", "why"),  # type: ignore[arg-type]
        )
        for t in local_tasks()
    }


def test_the_stage_tasks_take_the_tables_expected_outcome() -> None:
    tasks = local_stage_tasks(_table("blocked"))
    assert len(tasks) == len(local_tasks())
    first = tasks[0]
    assert (first.fixture, first.expect, first.reason) == ("local", "blocked", "commit_not_in_step")


def test_a_local_task_missing_from_the_table_is_refused() -> None:
    table = _table()
    table.pop(next(iter(table)))
    with pytest.raises(ValueError, match="unrated"):
        local_stage_tasks(table)


def test_a_page_read_is_shared_by_its_set_and_an_inbox_read_by_its_reply() -> None:
    """One read per distinct start: a page set's tasks share it; an inbox task's page holds its
    own reply text, so inbox reads group by reply."""
    groups = read_groups(local_tasks())
    assert sum(len(ids) for ids in groups.values()) == len(local_tasks())
    by_task = {i: key for key, ids in groups.items() for i in ids}
    page_sets = {t.start for t in local_tasks() if t.start.startswith("h")}
    for start in page_sets:
        assert len({by_task[t.task_id] for t in local_tasks() if t.start == start}) == 1
    inbox = [t for t in local_tasks() if t.start == "inbox"]
    assert all(start_key(t).startswith("inbox:") for t in inbox)


def test_the_summary_is_labelled_2a_on_the_stage_2_floor_with_no_valid_task_minimum() -> None:
    summary = summarize(local_stage_tasks(_table()), [], not_started=0, runs_per_task=3)
    assert summary.report["set"] == LABEL == "2a"
    assert summary.report["memory"]["floor_mb"] == FLOOR_MB == 4096
    assert "valid_tasks" not in summary.gates


def test_the_out_of_step_tasks_are_listed_in_the_summary() -> None:
    summary = summarize(local_stage_tasks(_table()), [], not_started=0, runs_per_task=3)
    assert summary.report["out_of_step"] == dict(OUT_OF_STEP)


def test_the_harness_never_takes_the_session_or_starts_a_monitor() -> None:
    tree = ast.parse(Path(stage2a.__file__).read_text(encoding="utf-8"))
    called = {
        getattr(node.func, "attr", getattr(node.func, "id", ""))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & {"acquire_session", "start_monitor", "HookMonitor", "LiveWorld"}
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    banned = ("steerai.session", "steerai.loop2.live_world", "steerai.platform")
    assert not [m for m in modules if m.startswith(banned) or m.endswith("monitor")]


def test_the_out_of_step_file_is_plain_json() -> None:
    assert isinstance(json.loads(stage2a.OUT_OF_STEP_FILE.read_text(encoding="utf-8")), dict)


def test_the_2a_table_is_built_by_stage_2s_own_table_builder(tmp_path: Path) -> None:
    from tests.loop2.test_stage2_expected import TYPE_ENTER, read, saved  # noqa: PLC0415

    task = local_tasks()[0]
    (tmp_path / f"{task.task_id}.json").write_bytes(saved(read()))
    rows = table_from_reads({task.task_id: TYPE_ENTER}, tmp_path, tasks=local_tasks())
    assert [r.task_id for r in rows] == [task.task_id]
    with pytest.raises(ValueError, match="acts"):
        table_from_reads({"no-such-task": TYPE_ENTER}, tmp_path, tasks=local_tasks())
