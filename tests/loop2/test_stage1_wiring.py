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
    accept,
    classify,
    gallery_tasks,
    large_tasks,
    page_check,
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
