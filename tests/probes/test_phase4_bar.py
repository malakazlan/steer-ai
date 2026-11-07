"""The Phase 4 bar verdict (section 12): repeats end `done` with no planner call, and from the
trusted state on with no model call at all."""

from __future__ import annotations

from typing import Any

from steerai.probes.phase4_bar import verdict


def _row(status: str = "done", planner: int = 0, judge: int = 0) -> dict[str, Any]:
    return {"status": status, "planner_calls": planner, "goal_match_calls": judge}


def _measured() -> list[dict[str, Any]]:
    """The live run of 2026-11-08: one fill, two probation repeats, three trusted ones."""
    return [_row(planner=1, judge=1), _row(judge=1), _row(judge=1), _row(), _row(), _row()]


def test_the_measured_run_meets_the_bar() -> None:
    line, passed = verdict(_measured())
    assert passed
    assert line == ("repeats 5: done 5, planner calls 0 in 5, all model calls 0 in 3, bar met")


def test_a_repeat_that_plans_misses_the_bar() -> None:
    rows = _measured()
    rows[2] = _row(planner=1, judge=1)
    assert not verdict(rows)[1]


def test_a_trusted_repeat_that_judges_misses_the_bar() -> None:
    rows = _measured()
    rows[4] = _row(judge=1)
    assert not verdict(rows)[1]


def test_a_stopped_repeat_or_fill_misses_the_bar() -> None:
    rows = _measured()
    rows[5] = _row(status="stopped")
    assert not verdict(rows)[1]
    rows = _measured()
    rows[0] = _row(status="stopped", planner=1, judge=1)
    assert not verdict(rows)[1]


def test_no_rows_misses_the_bar() -> None:
    assert not verdict([])[1]
