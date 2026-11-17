"""Stage 1's harness wired to the expected outcomes (`stage1_expected`): the gallery and large
tasks, their expected outcome and reason, and each page's own pass check. Pure: the saved read
is a file; no Chrome, no child, no input."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from steerai.loop2.stage1 import (
    THREE_D,
    RunRecord,
    StageTask,
    _page_checks,
    accept,
    classify,
    diagnostic_row,
    diagnostic_tasks,
    gallery_tasks,
    large_tasks,
    page_check,
    page_row,
    read_page,
    tasks_for,
)
from steerai.loop2.stage1_expected import COMMIT_REASON, Expected, gallery_expected
from steerai.probes.gallery_page import LARGE_TARGET
from steerai.probes.task_suite import GALLERY
from steerai.world.read_json import observed_from_json

READ = Path(__file__).resolve().parents[1] / "world" / "fixtures" / "gallery" / "page_read.json"


def _expected() -> dict[str, Expected]:
    """The gallery rows' expected outcomes, rated on the gallery's saved read."""
    return gallery_expected(observed_from_json(READ.read_text("utf-8")))


def test_every_gallery_row_is_a_task_with_its_expected_outcome_and_reason() -> None:
    tasks = gallery_tasks(_expected())
    assert [t.task_id for t in tasks] == [t.task_id for t in GALLERY]
    goals = {t.task_id: t.goal for t in GALLERY}
    for task in tasks:
        assert (task.goal, task.fixture, task.page) == (
            goals[task.task_id],
            "gallery",
            "/page.html",
        )
    by_id = {t.task_id: t for t in tasks}
    for blocked in ("g17", "g18", "g19"):
        assert (by_id[blocked].expect, by_id[blocked].reason) == ("blocked", COMMIT_REASON)
    assert (by_id["g01"].expect, by_id["g01"].reason) == ("never_done", None)
    done = [t.task_id for t in tasks if t.expect == "done"]
    assert len(done) == 20
    assert all(by_id[t].reason is None for t in done)


def test_the_large_tasks_are_expected_done_on_the_large_page() -> None:
    assert [(t.task_id, t.goal, t.fixture, t.page, t.expect, t.reason) for t in large_tasks()] == [
        ("lg1", f"reserve item {LARGE_TARGET}", "large", "/large.html", "done", None),
        ("lg2", "sort by size", "large", "/large.html", "done", None),
    ]


def test_the_set_names_choose_the_tasks() -> None:
    expected = _expected()
    assert tasks_for("3d", expected) == THREE_D
    assert tasks_for("gallery", expected) == gallery_tasks(expected)
    assert tasks_for("large", expected) == large_tasks()
    with pytest.raises(ValueError, match="unknown set"):
        tasks_for("elsewhere", expected)


def test_large_acceptance_still_fails_closed() -> None:
    """Rev 4.3a / #366 P5: the target listed on the turn it is acted on is not checked yet."""
    task = large_tasks()[0]
    final = {"status": "done", "reason": None, "used": {"max_actions": 1}}
    runs = [
        classify(task, RunRecord(task.task_id, n, final, True, True, True, None)) for n in (1, 2, 3)
    ]
    summary = accept(large_tasks()[:1], runs, controls=None, runs_per_task=3, label="large")
    assert summary.gates["large_listed"] is False
    assert summary.accepted is False


# ---- a blocked gallery row passes only on the runner's own reason ----------------------------


def _blocked_run(reason: str) -> RunRecord:
    final = {"status": "blocked", "reason": reason, "used": {"max_actions": 0}}
    return RunRecord("g17", 1, final, True, True, False, None)


def test_a_blocked_row_passes_on_commit_not_in_step() -> None:
    g17 = next(t for t in gallery_tasks(_expected()) if t.task_id == "g17")
    assert classify(g17, _blocked_run(COMMIT_REASON)).verdict == "pass"
    assert classify(g17, _blocked_run("not_in_step")).verdict == "fail"


# ---- each page's own pass check, from its log alone ------------------------------------------


def _log(*entries: dict[str, object]) -> str:
    return json.dumps(list(entries))


@pytest.mark.parametrize(
    ("task_id", "raw", "ok"),
    [
        ("g03", _log({"row": "g03", "on": True}), True),
        ("g03", _log({"row": "g03", "on": False}), False),
        ("g03", _log({"row": "g03", "on": True}, {"row": "g02", "on": True}), False),
        ("g03", _log({"row": "g03", "on": True, "untrusted": True}), False),
        ("g03", "[]", False),
        ("lg1", _log({"row": "large-reserve", "item": LARGE_TARGET, "on": True}), True),
        ("lg1", _log({"row": "large-details", "item": 3}), False),
        (
            "lg2",
            _log({"row": "large-menu", "open": True}, {"row": "large-sort", "value": "size"}),
            True,
        ),
        ("lg2", _log({"row": "large-menu", "open": True}), False),
    ],
)
def test_the_page_check_reads_the_pages_own_log(task_id: str, raw: str, ok: bool) -> None:
    tasks = {t.task_id: t for t in (*gallery_tasks(_expected()), *large_tasks())}
    assert page_check(tasks[task_id], raw) is ok


def test_a_large_log_of_another_shape_is_an_error_never_a_pass() -> None:
    with pytest.raises(ValueError, match="large log"):
        page_check(large_tasks()[0], '[{"row": "elsewhere"}]')


def test_a_3d_task_has_no_page_check_here() -> None:
    with pytest.raises(ValueError, match="no page check"):
        page_check(THREE_D[0], "[]")


# ---- [V2.84] (2): the page check records what it observed, select rows split outcome/path ---


def _tasks() -> dict[str, StageTask]:
    return {t.task_id: t for t in (*gallery_tasks(_expected()), *large_tasks())}


ROME: dict[str, object] = {"row": "g13", "value": "Europe/Rome"}
TOKYO: dict[str, object] = {"row": "g13", "value": "Asia/Tokyo"}


def test_one_trusted_rome_event_is_ok_with_path_one() -> None:
    got = read_page(_tasks()["g13"], _log(ROME), dom="Europe/Rome")
    assert (got.ok, got.kind, got.events) == (True, "ok", 1)
    assert json.loads(got.observed) == ROME


def test_three_events_ending_trusted_rome_is_an_ok_outcome_with_path_three() -> None:
    raw = _log({"row": "g13", "value": "Europe/Paris"}, TOKYO, ROME)
    got = read_page(_tasks()["g13"], raw, dom="Europe/Rome")
    assert (got.ok, got.kind, got.events) == (True, "ok", 3)
    assert page_check(_tasks()["g13"], raw, dom="Europe/Rome") is True


def test_a_log_ending_tokyo_over_a_rome_dom_is_wrong_value() -> None:
    got = read_page(_tasks()["g13"], _log(ROME, ROME, TOKYO), dom="Europe/Rome")
    assert (got.ok, got.kind, got.events) == (False, "wrong_value", 3)
    assert json.loads(got.observed) == TOKYO


def test_h2_a_dom_other_than_rome_while_the_log_read_rome_is_dom_value_differs() -> None:
    """The popup was open: the log holds [Rome, X], the select holds X, code read Rome."""
    got = read_page(_tasks()["g13"], _log(ROME, TOKYO), dom="Asia/Tokyo")
    assert (got.ok, got.kind, got.events, got.dom) == (False, "dom_value_differs", 2, "Asia/Tokyo")
    task = _tasks()["g13"]
    record = RunRecord("g13", 1, DONE_FINAL, True, True, got.ok, None, page=got)
    assert classify(task, record).verdict == "false_done"


def test_h1_extra_events_ending_rome_with_a_rome_dom_is_ok_path_three() -> None:
    got = read_page(_tasks()["g13"], _log(ROME, TOKYO, ROME), dom="Europe/Rome")
    assert (got.ok, got.kind, got.events, got.dom) == (True, "ok", 3, "Europe/Rome")
    assert [(e.row, e.value, e.trusted) for e in got.entries] == [
        ("g13", "Europe/Rome", True), ("g13", "Asia/Tokyo", True), ("g13", "Europe/Rome", True),
    ]  # fmt: skip


def test_an_unread_dom_on_a_select_row_fails_closed() -> None:
    got = read_page(_tasks()["g13"], _log(ROME), dom=None)
    assert (got.ok, got.kind) == (False, "unread")


def test_the_record_keeps_every_entry_and_the_dom_for_the_out_dir() -> None:
    got = read_page(_tasks()["g13"], _log(ROME, {**TOKYO, "untrusted": True}), dom="Europe/Rome")
    row = page_row(got)
    assert row["dom_value"] == "Europe/Rome"
    assert row["entries"] == [
        {"row": "g13", "value": "Europe/Rome", "trusted": True},
        {"row": "g13", "value": "Asia/Tokyo", "trusted": False},
    ]


def test_a_stray_entry_of_a_typed_row_keeps_no_value() -> None:
    raw = _log({"row": "g22", "value": "secret"}, ROME)
    got = read_page(_tasks()["g13"], raw, dom="Europe/Rome")
    assert got.entries[0].value is None


def test_an_untrusted_last_event_is_untrusted() -> None:
    got = read_page(_tasks()["g13"], _log(ROME, {**ROME, "untrusted": True}), dom="Europe/Rome")
    assert (got.ok, got.kind, got.observed, got.events) == (False, "untrusted", "untrusted", 2)


def test_no_event_and_a_bad_log_on_a_select_row() -> None:
    assert read_page(_tasks()["g13"], "[]", dom="").kind == "events:0"
    assert read_page(_tasks()["g13"], "not json", dom="").kind == "unread"


@pytest.mark.parametrize("task_id", ["g12", "g14"])
def test_every_select_row_takes_the_outcome_rule(task_id: str) -> None:
    row: dict[str, dict[str, object]] = {
        "g12": {"row": "g12", "value": "Kenya"},
        "g14": {"row": "g14", "value": "Teal"},
    }
    other: dict[str, object] = {"row": task_id, "value": "Other"}
    value = str(row[task_id]["value"])
    assert read_page(_tasks()[task_id], _log(other, row[task_id]), dom=value).ok is True


def test_a_non_select_row_keeps_its_check_and_names_the_failure() -> None:
    task = _tasks()["g03"]
    two = read_page(task, _log({"row": "g03", "on": True}, {"row": "g03", "on": True}))
    assert (two.ok, two.kind, two.observed, two.events) == (False, "events:2", "events:2", 2)
    assert read_page(task, _log({"row": "g03", "on": False})).kind == "wrong_value"
    assert read_page(task, _log({"row": "g03", "on": True, "untrusted": True})).kind == "untrusted"
    assert read_page(task, "[]").kind == "events:0"
    assert read_page(task, "garbage").kind == "unread"


def test_a_typed_value_is_never_kept_in_the_observed_string() -> None:
    got = read_page(_tasks()["g22"], _log({"row": "g22", "value": "someone's secret"}))
    assert (got.kind, got.observed) == ("wrong_value", "redacted:typed_value")


def test_a_large_task_reads_closed() -> None:
    got = read_page(_tasks()["lg2"], _log({"row": "large-menu", "open": True}))
    assert (got.ok, got.kind, got.observed, got.events) == (False, "wrong_value", "large", None)


DONE_FINAL = {"status": "done", "reason": None, "used": {"max_actions": 1}}


def _run(task: StageTask, raw: str, run: int = 1, dom: str | None = None) -> tuple[RunRecord, bool]:
    page = read_page(task, raw, dom=dom)
    return RunRecord(task.task_id, run, DONE_FINAL, True, True, page.ok, None, page=page), page.ok


def test_a_false_done_keeps_its_observation_and_path_is_not_a_false_done() -> None:
    task = _tasks()["g13"]
    bad, _ = _run(task, _log(ROME, TOKYO), dom="Asia/Tokyo")
    got = classify(task, bad)
    assert (got.verdict, got.page) == ("false_done", bad.page)
    many, _ = _run(task, _log(TOKYO, ROME, ROME), dom="Europe/Rome")
    assert classify(task, many).verdict == "pass"


def test_the_summary_reports_path_apart_from_the_verdicts() -> None:
    task = _tasks()["g13"]
    runs = [
        classify(task, _run(task, raw, i, "Europe/Rome")[0])
        for i, raw in enumerate((_log(ROME), _log(TOKYO, ROME, ROME)), 1)
    ]
    got = _page_checks(runs)
    assert got == {"kinds": {}, "select_path_events": {"g13": [1, 3]}, "select_path_over_one": 1}
    assert page_row(runs[1].page) == {
        "observed": json.dumps(ROME, sort_keys=True),
        "check_kind": "ok",
        "path_events": 3,
        "dom_value": "Europe/Rome",
        "entries": [
            {"row": "g13", "value": "Asia/Tokyo", "trusted": True},
            {"row": "g13", "value": "Europe/Rome", "trusted": True},
            {"row": "g13", "value": "Europe/Rome", "trusted": True},
        ],
    }


def test_a_diagnostic_names_one_task_and_carries_no_gate() -> None:
    tasks = tuple(_tasks().values())
    assert [t.task_id for t in diagnostic_tasks(tasks, "g13")] == ["g13"]
    assert diagnostic_tasks(tasks, None) == tasks
    with pytest.raises(ValueError, match="no stage 1 task"):
        diagnostic_tasks(tasks, "nope")
    row = diagnostic_row(
        [classify(_tasks()["g13"], _run(_tasks()["g13"], _log(ROME), dom="Europe/Rome")[0])]
    )
    assert row["diagnostic"]["uncounted"] is True
    assert "gates" not in row
    assert "accepted" not in row
