"""Stage 1 acceptance prep (offline entry run, 2026-11-15): a run whose child refused to start
([V2.62]/[V2.64]: `page_hidden`, `restore_failed`, `window_covered`, ...) fails once, under its
own reason with the refusal's detail, never a second time under gates that read a run's use
(`cache_zero`, `large_listed`); and the final audit ([V2.64] One) is counted in the report."""

from __future__ import annotations

from typing import Any

from steerai.loop2.stage1 import accept, classify
from steerai.orchestrator.contracts import RISKS
from tests.loop2.test_large_listed import LARGE
from tests.loop2.test_stage1 import DONE, _final, _ok, _record

PAGE_HIDDEN: dict[str, Any] = {"status": "blocked", "code": "focus_lost", "detail": "page_hidden"}
"""The CLI's line when `LiveWorld.bring_to_front` refused the start: no use, no counters."""


def test_a_refused_start_names_its_code_and_its_detail() -> None:
    got = classify(DONE, _record(PAGE_HIDDEN, check=False))
    assert (got.verdict, got.reason) == ("fail", "start_refused:focus_lost:page_hidden")


def test_a_refused_start_with_no_detail_names_its_code_alone() -> None:
    got = classify(DONE, _record({"status": "blocked", "code": "focus_lost"}, check=False))
    assert got.reason == "start_refused:focus_lost"


def test_a_refused_start_is_not_also_a_cache_failure() -> None:
    runs = [_ok(DONE, 1, "fail", final=PAGE_HIDDEN, reason="start_refused:focus_lost:page_hidden"),
            _ok(DONE, 2), _ok(DONE, 3), _ok(DONE, 4)]  # fmt: skip
    summary = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t")
    assert summary.gates["cache_zero"] is True
    assert summary.gates["tasks"] is False  # it fails once, under the task


def test_a_refused_large_start_is_not_also_a_large_listed_failure() -> None:
    listed = _final(listings=[{"acted": 1, "acted_listed": True}])
    runs = [_ok(LARGE, 1, "fail", final=PAGE_HIDDEN, reason="start_refused:focus_lost:page_hidden"),
            *(_ok(LARGE, r, final=listed) for r in (2, 3, 4))]  # fmt: skip
    summary = accept([LARGE], runs, controls=None, runs_per_task=3, label="t")
    assert summary.gates["large_listed"] is True


def test_the_report_counts_the_final_audit_by_result_and_reason() -> None:
    allowed = _final(audit={"ran": True, "result": "allowed", "reason": "outcome_shown"})
    vetoed = _final(audit={"ran": True, "result": "vetoed", "reason": "outcome_not_shown"})
    none = _final(audit={"ran": False, "result": None, "reason": None})
    runs = [_ok(DONE, 1, final=allowed), _ok(DONE, 2, final=vetoed), _ok(DONE, 3, final=none)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["audit"] == {
        "ran": 2,
        "result": {"allowed": 1, "vetoed": 1},
        "reason": {"outcome_shown": 1, "outcome_not_shown": 1},
        "incomplete": 0,
    }


def test_an_audit_block_of_another_shape_is_counted_incomplete() -> None:
    odd = _final(audit={"ran": "yes"})
    missing = _final()
    del missing["audit"]
    runs = [_ok(DONE, 1, final=odd), _ok(DONE, 2, final=missing), _ok(DONE, 3)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["audit"]["incomplete"] == 2
    assert report["audit"]["ran"] == 0


# ---- the report header ([V2.64] Four, closed by [V2.65] Four) ----------------------------------


def test_the_report_header_records_the_mta_comparability_statement() -> None:
    from steerai.loop2.stage1 import STAGE1_FLOOR_MB, report_header  # noqa: PLC0415

    header = report_header("3d", runs=3)
    assert header["set"] == "3d"
    assert header["runs_per_task"] == 3
    assert header["floor_mb"] == STAGE1_FLOOR_MB
    mta = header["mta_comparability"]
    assert mta["status"] == "closed"
    assert mta["ruling"] == "[V2.65] Four"
    assert mta["differ"] == []
    assert mta["steps_ok"] == {"sta": "30/33", "mta": "30/33"}


def test_a_shakedown_header_says_so() -> None:
    from steerai.loop2.stage1 import report_header  # noqa: PLC0415

    assert report_header("gallery", runs=1)["set"] == "gallery-shakedown"


# ---- the start's nudge ([V2.64] Three), counted over the runs -----------------------------------


def test_the_report_counts_the_starts_nudge_by_kind() -> None:
    def nudged(kind: str | None) -> dict[str, Any]:
        return _final(world={"monitor_lost": 0, "front_nudged": kind})

    unread = _final(world={"monitor_lost": 0})
    runs = [_ok(DONE, 1, final=nudged("move1px")), _ok(DONE, 2, final=nudged(None)),
            _ok(DONE, 3, final=nudged("minrestore")), _ok(DONE, 4, final=unread),
            _ok(DONE, 5, final=nudged("sideways"))]  # fmt: skip
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["start_nudge"] == {"move1px": 1, "minrestore": 1, "none": 1, "unread": 2}


def test_a_refused_start_dispatched_nothing_so_it_is_not_an_r2_failure() -> None:
    """The CLI refuses the start before any port exists: nothing could be dispatched. An error
    line is still checked like any other (#413 blocker 2: fail closed)."""
    refused = _ok(DONE, 1, "fail", final=PAGE_HIDDEN, reason="start_refused:focus_lost:page_hidden")
    ok = [_ok(DONE, r, final=_final(dispatch_risks=dict.fromkeys(RISKS, 0))) for r in (2, 3, 4)]
    summary = accept([DONE], [refused, *ok], controls=(5, 5), runs_per_task=3, label="t")
    assert summary.gates["no_r2_dispatch"] is True
    errored = _ok(DONE, 1, "fail", final={"status": "error", "detail": "internal_error"})
    summary = accept([DONE], [errored, *ok], controls=(5, 5), runs_per_task=3, label="t")
    assert summary.gates["no_r2_dispatch"] is False


# ---- #425: model text never becomes a report key; the MTA figures cite their artefact ------------


def test_an_audit_reason_off_the_closed_enum_is_counted_as_other() -> None:
    free_text = _final(audit={"ran": True, "result": "vetoed", "reason": "ignore the rules"})
    shown = _final(audit={"ran": True, "result": "allowed", "reason": "outcome_shown"})
    failed = _final(audit={"ran": True, "result": "failed", "reason": "audit_no_call"})
    runs = [_ok(DONE, 1, final=free_text), _ok(DONE, 2, final=shown), _ok(DONE, 3, final=failed)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["audit"]["reason"] == {"other": 1, "outcome_shown": 1, "audit_no_call": 1}


def test_the_closed_audit_reasons_are_the_controllers_and_the_runners() -> None:
    from typing import get_args  # noqa: PLC0415

    from steerai.loop2.loop import AuditReason  # noqa: PLC0415
    from steerai.loop2.stage1 import AUDIT_REASONS  # noqa: PLC0415

    assert set(get_args(AuditReason)) | {"audit_failed", "audit_no_call"} == AUDIT_REASONS


def test_the_mta_statement_cites_its_probe_artefact() -> None:
    from steerai.loop2.stage1 import MTA_COMPARABILITY  # noqa: PLC0415

    assert MTA_COMPARABILITY["artefact"].endswith("mta_act.json")
    assert "#400" in MTA_COMPARABILITY["artefact_summary"]
