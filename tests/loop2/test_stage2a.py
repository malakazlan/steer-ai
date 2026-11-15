"""Stage 2's group 2a (stage 2 plan rev 4.4, [V2.63] Q1): the pooled set's local tasks, with the
same harness and gates as 2b and no network. The native tasks that need no dialog and no second
window are in; the rest wait for step 7 and are listed, not run (data, never a rule naming an
app in code)."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from steerai.loop2 import stage2a
from steerai.loop2.stage1 import FLOOR_MB
from steerai.loop2.stage1_expected import Expected
from steerai.loop2.stage2_expected import (
    DecidingFacts,
    TableRow,
    load_acts,
    load_table,
    load_unrated,
    table_from_reads,
)
from steerai.loop2.stage2a import (
    LABEL,
    OUT_OF_STEP,
    calculator_window,
    check_table,
    load_out_of_step,
    local_stage_tasks,
    local_tasks,
    rate_2a,
    read_groups,
    start_key,
    summarize,
)
from steerai.probes.layer_b_pages import InvalidRun
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


def test_the_committed_acts_cover_every_2a_task_rated_or_not() -> None:
    text = stage2a.ACTS.read_text(encoding="utf-8")
    assert set(load_acts(text)) | set(load_unrated(text)) == {t.task_id for t in local_tasks()}


def test_rating_refuses_a_2a_task_with_no_acts(tmp_path: Path) -> None:
    acts = load_acts(stage2a.ACTS.read_text(encoding="utf-8"))
    acts.pop(next(iter(acts)))
    with pytest.raises(ValueError, match="no_acts"):
        rate_2a(acts, tmp_path)


def test_the_committed_table_loads_and_rates_every_rated_2a_task() -> None:
    text = stage2a.TABLE.read_text(encoding="utf-8")
    table = load_table(text)
    assert set(table) == {t.task_id for t in local_tasks()} - set(load_unrated(text))
    assert local_stage_tasks(table, unrated=load_unrated(text))


def test_the_unrated_2a_tasks_are_kept_out_of_the_table_and_the_run() -> None:
    text = stage2a.TABLE.read_text(encoding="utf-8")
    unrated = load_unrated(text)
    assert unrated == load_unrated(stage2a.ACTS.read_text(encoding="utf-8"))
    assert set(load_table(text)) | set(unrated) == {t.task_id for t in local_tasks()}
    stages = local_stage_tasks(load_table(text), unrated=unrated)
    held = {s.task_id: s.expect for s in stages if s.task_id in unrated}
    assert held == dict.fromkeys(unrated, "unrated")  # [V2.65] (2): run, never exempt
    assert len(stages) == len(local_tasks())
    summary = summarize(stages, [], not_started=0, runs_per_task=3, unrated=unrated)
    assert summary.report["unrated"] == unrated


# ---- #408 review: blockers 1-4 -----------------------------------------------------------------


def test_the_committed_table_is_pinned_and_passes_its_check() -> None:
    text = stage2a.TABLE.read_text(encoding="utf-8")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == stage2a.TABLE_SHA256
    check_table(text)


def test_a_table_that_moves_a_task_into_unrated_is_refused() -> None:
    """Unrated must never hide a failing run: a table whose rated set or unrated map is not the
    committed one is refused, before any run."""
    raw = json.loads(stage2a.TABLE.read_text(encoding="utf-8"))
    rated = next(k for k, v in raw.items() if "expected" in v)
    raw[rated] = {"unrated": "target_after_opener"}
    with pytest.raises(ValueError, match="table"):
        check_table(json.dumps(raw, indent=2) + "\n")


def test_a_table_that_differs_by_any_byte_is_refused() -> None:
    text = stage2a.TABLE.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="table"):
        check_table(text.replace('"why": "a tab', '"why": "a  tab', 1))


def test_a_held_task_with_a_table_row_is_refused() -> None:
    table = _table()
    held = next(iter(table))
    with pytest.raises(ValueError, match="held"):
        local_stage_tasks(table, unrated={held: "target_after_opener"})


def test_a_held_id_that_is_not_a_2a_task_is_refused() -> None:
    with pytest.raises(ValueError, match="held"):
        local_stage_tasks(_table(), unrated={"no-such-task": "target_after_opener"})


def test_the_pass_check_is_independent_of_the_verifier() -> None:
    """Blocker 2: no import from the verifier, and `passed` only ever asked about a "done"."""
    tree = ast.parse(Path(stage2a.__file__).read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in modules if "verif" in m]
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "passed"
    ]
    assert calls
    for call in calls:
        status = call.args[1]
        assert isinstance(status, ast.Constant)
        assert status.value == "done"


class Window:
    def __init__(self, title: str, hwnd: int) -> None:
        self.title = title
        self.hwnd = hwnd


def test_a_calculator_already_open_is_refused_before_launching() -> None:
    launched: list[int] = []
    with (
        pytest.raises(InvalidRun, match="calculator_already_open"),
        calculator_window(
            title="Calculator",
            windows=lambda: [Window("Calculator", 7)],
            launch=lambda: launched.append(1),
            close=lambda _hwnd: None,
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        ),
    ):
        pass
    assert launched == []


def test_a_calculator_that_shows_late_is_closed_on_the_not_shown_path() -> None:
    """Blocker 4: the window it launched is closed even when it shows after the deadline."""
    t = [0.0]
    shown: list[list[Window]] = [[]]
    closed: list[int] = []

    def sleep(seconds: float) -> None:
        t[0] += seconds
        if t[0] > 25.0:
            shown[0] = [Window("Calculator", 9)]

    with (
        pytest.raises(InvalidRun, match="calculator_not_shown"),
        calculator_window(
            title="Calculator",
            windows=lambda: shown[0],
            launch=lambda: None,
            close=closed.append,
            sleep=sleep,
            clock=lambda: t[0],
            settle_s=10.0,
        ),
    ):
        pass
    assert closed == [9]


def test_a_calculator_it_launched_is_closed_after_use() -> None:
    closed: list[int] = []
    shown = [[], [Window("Calculator", 5)]]
    with calculator_window(
        title="Calculator",
        windows=lambda: shown.pop(0) if len(shown) > 1 else shown[0],
        launch=lambda: None,
        close=closed.append,
        sleep=lambda _s: None,
        clock=lambda: 0.0,
    ) as hwnd:
        assert hwnd == 5
    assert closed == [5]


def test_only_the_committed_2a_acts_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#428 (8): the 2a table check also pins the acts file it reads."""
    edited = tmp_path / "acts.json"
    edited.write_text(stage2a.ACTS.read_text(encoding="utf-8") + " ", encoding="utf-8")
    monkeypatch.setattr(stage2a, "ACTS", edited)
    with pytest.raises(ValueError, match="not the committed 2a acts"):
        stage2a.check_table(stage2a.TABLE.read_text(encoding="utf-8"))
