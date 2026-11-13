"""The suite's four-way outcome (V2 §11; packet v2-step1-observability §D): Steer's verdict
crossed with the independent read, so a false stop is visible and a false done can never hide
in an unread, an extra open or a post-run invalid (hunter OB-04/05/06/19/20/37)."""

from __future__ import annotations

import pytest

from steerai.probes.suite_outcome import (
    STAGE_BUCKETS,
    Run,
    outcome,
    planner_output_tokens,
    split_ms,
)


def _run(status: str | None, independent: str = "ok", **kw: object) -> Run:
    return Run(status=status, independent=independent, **kw)  # type: ignore[arg-type]  # the literals under test


@pytest.mark.parametrize(
    ("independent", "expected"),
    [("ok", "true_done"), ("not", "false_done"), ("unread", "false_done")],
)
def test_done_is_true_only_when_the_independent_read_agrees(
    independent: str, expected: str
) -> None:
    assert outcome(_run("done", independent)) == expected


def test_done_with_an_open_the_suite_blocked_is_never_a_true_done() -> None:
    assert outcome(_run("done", "ok", opens=1)) == "false_done"


@pytest.mark.parametrize("status", ["stopped", "blocked", "paused"])
@pytest.mark.parametrize(
    ("independent", "expected"),
    [("ok", "false_stop"), ("not", "true_stop"), ("unread", "stop_unread")],
)
def test_a_stop_is_false_when_the_page_shows_the_goal(
    status: str, independent: str, expected: str
) -> None:
    assert outcome(_run(status, independent, detail="unfinished")) == expected


@pytest.mark.parametrize("detail", ["takeover", "killed", "session_locked"])
def test_the_person_interrupting_is_its_own_bucket_whatever_the_page_shows(detail: str) -> None:
    for independent in ("ok", "not", "unread"):
        assert outcome(_run("paused", independent, detail=detail)) == "interrupted"
        assert outcome(_run("blocked", independent, detail=detail)) == "interrupted"


@pytest.mark.parametrize("status", ["error", None, "", "something_new"])
def test_an_error_or_an_unknown_status_is_an_error_never_a_stop(status: str | None) -> None:
    assert outcome(_run(status, "ok")) == "error"


def test_a_done_found_invalid_after_the_run_still_counts_against_the_bar() -> None:
    assert outcome(_run("done", "ok", post_invalid=True)) == "false_done"
    assert outcome(_run("stopped", "ok", post_invalid=True)) == "false_stop"


def test_a_pre_run_invalid_measured_nothing() -> None:
    assert outcome(_run(None, "unread", pre_invalid=True)) == "invalid"


# ---- the wall-time split and the planner's output (packet §D; hunter OB-23/38/39) ----------


def _records() -> list[dict[str, object]]:
    return [
        {"kind": "plan_read", "read_ms": 2000},
        {"kind": "plan", "latency_ms": 7000.0, "output_tokens": 500},
        {"kind": "goal_match", "latency_ms": 1500.0},
        {
            "kind": "step",
            "latency_ms": {"observe": 100.0, "gate": 10.0, "decide": 0.0, "act": 60.0},
        },
        {
            "kind": "step",
            "latency_ms": {"observe": 300.0, "settle": 2000.0, "act": 80.0, "verify": 10000.0},
        },
    ]


def test_every_millisecond_lands_in_exactly_one_bucket() -> None:
    split = split_ms(_records(), seconds=25.0)
    assert split == {
        "read": 2000,
        "model": 8500,
        "observe": 400,
        "gate": 10,
        "decide": 0,
        "act": 140,
        "settle": 2000,
        "verify_wait": 10000,
        "other": 25000 - 23050,
    }


def test_a_split_larger_than_the_run_is_double_counting() -> None:
    with pytest.raises(ValueError, match="double"):
        split_ms(_records(), seconds=20.0)


def test_every_stage_the_loop_measures_has_a_bucket() -> None:
    assert set(STAGE_BUCKETS) == {"observe", "gate", "decide", "act", "settle", "verify"}


def test_an_unknown_stage_is_refused_never_dropped() -> None:
    records = [{"kind": "step", "latency_ms": {"teleport": 5.0}}]
    with pytest.raises(ValueError, match="teleport"):
        split_ms(records, seconds=1.0)


def test_output_tokens_count_a_failed_plan_as_incomplete() -> None:
    assert planner_output_tokens(_records()) == (500, False)
    records = [*_records(), {"kind": "plan", "latency_ms": 900.0}]
    assert planner_output_tokens(records) == (500, True)
