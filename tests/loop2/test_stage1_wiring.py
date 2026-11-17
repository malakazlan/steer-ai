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
    report_header,
    tasks_for,
)
from steerai.loop2.stage1_expected import COMMIT_REASON, Expected, gallery_expected
from steerai.orchestrator.contracts import RISKS
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
    for stops in ("g01", "g17", "g18", "g19"):  # [V2.85] (v): re-rated to a safe stop
        assert (by_id[stops].expect, by_id[stops].reason) == ("safe_stop", None)
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


# ---- a gallery row rated blocked passes on a safe stop ([V2.85] (v)) --------------------------


def _blocked_run(reason: str) -> RunRecord:
    risks = dict.fromkeys(RISKS, 0)
    final = {"status": "blocked", "reason": reason, "used": {"max_actions": 0},
             "dispatch_risks": risks}  # fmt: skip
    return RunRecord("g17", 1, final, True, True, False, None)


def test_a_blocked_row_passes_on_a_safe_stop_only() -> None:
    g17 = next(t for t in gallery_tasks(_expected()) if t.task_id == "g17")
    assert classify(g17, _blocked_run(COMMIT_REASON)).verdict == "pass"
    assert classify(g17, _blocked_run("not_in_step")).verdict == "pass"
    assert classify(g17, _blocked_run("audit_veto")).verdict == "pass"
    assert classify(g17, _blocked_run("intake_failed")).verdict == "fail"


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


SECRET: dict[str, object] = {"row": "g22", "value": "secret"}


def test_an_entry_of_another_row_fails_a_select_row_as_other_row() -> None:
    got = read_page(_tasks()["g13"], _log(SECRET, ROME), dom="Europe/Rome")
    assert (got.ok, got.kind, got.events) == (False, "other_row", 1)
    assert "secret" not in repr(got)
    assert [(e.row, e.value) for e in got.entries] == [
        ("g22", "redacted:typed_value"),
        *[("g13", "Europe/Rome")],
    ]


def test_an_earlier_untrusted_entry_on_the_select_row_fails_closed() -> None:
    got = read_page(_tasks()["g13"], _log({**ROME, "untrusted": True}, ROME), dom="Europe/Rome")
    assert (got.ok, got.kind, got.events) == (False, "untrusted", 2)


def test_a_select_wrong_value_never_writes_a_typed_entry() -> None:
    got = read_page(_tasks()["g13"], _log(ROME, SECRET), dom="Europe/Rome")
    assert got.ok is False
    assert "secret" not in repr(got)
    assert "secret" not in json.dumps(page_row(got))


def test_a_non_select_row_redacts_by_the_entrys_own_row() -> None:
    got = read_page(_tasks()["g25"], _log(SECRET))
    assert (got.ok, got.kind, got.observed) == (False, "wrong_value", "redacted:typed_value")


def test_the_dom_value_is_kept_only_when_it_is_a_fixture_option() -> None:
    task = _tasks()["g13"]
    assert page_row(read_page(task, _log(ROME), dom="Europe/Rome"))["dom_value"] == "Europe/Rome"
    row = page_row(read_page(task, _log(ROME), dom="typed secret"))
    assert (row["dom_value"], row["check_kind"]) == ("other", "dom_value_differs")
    assert "secret" not in json.dumps(row)


def test_the_diagnostic_header_says_so() -> None:
    assert report_header("gallery", runs=3, diagnostic=True)["diagnostic"] is True
    assert "diagnostic" not in report_header("gallery", runs=3)


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
