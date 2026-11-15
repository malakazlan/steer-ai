"""Stage 1 acceptance prep (offline entry run, 2026-11-15), revised by [V2.67] C: a run whose
child refused to start ([V2.62]/[V2.64]: `page_hidden`, `restore_failed`, `window_covered`; the
session, the price file, ...) dispatched nothing and never began, so it cannot be scored: it is
an infrastructure void under its own reason with the refusal's detail, counted in the 1-in-10
void limit and split by cause in the report, never under the gates that read a run's use
(`cache_zero`, `large_listed`, `no_r2_dispatch`); and the final audit ([V2.64] One) is counted
in the report."""

from __future__ import annotations

import base64
import io
import subprocess
from pathlib import Path
from typing import Any

from steerai.loop2.stage1 import accept, classify
from steerai.orchestrator.contracts import RISKS
from tests.loop2.test_large_listed import LARGE
from tests.loop2.test_stage1 import DONE, _final, _ok, _record

PAGE_HIDDEN: dict[str, Any] = {"status": "blocked", "code": "focus_lost", "detail": "page_hidden"}
"""The CLI's line when `LiveWorld.bring_to_front` refused the start: no use, no counters."""


def test_a_refused_start_is_a_void_naming_its_code_and_its_detail() -> None:
    got = classify(DONE, _record(PAGE_HIDDEN, check=False))
    assert (got.verdict, got.reason) == ("void", "start_refused:focus_lost:page_hidden")


def test_a_price_error_before_the_start_is_a_void_too() -> None:
    final = {"status": "error", "code": "gate_blocked", "detail": "price_missing"}
    got = classify(DONE, _record(final, check=False))
    assert (got.verdict, got.reason) == ("void", "start_refused:gate_blocked:price_missing")


def test_an_error_line_with_a_use_block_stays_a_fail() -> None:
    """#438 blocker 3: an error after the task began (its line has `used`) is never a void."""
    final = {**_final(), "status": "error", "code": "gate_blocked", "detail": "x"}
    got = classify(DONE, _record(final, check=False))
    assert (got.verdict, got.reason) == ("fail", "error_after_start")


def test_an_internal_error_is_still_a_fail_not_a_void() -> None:
    final = {"status": "error", "code": None, "detail": "internal_error"}
    assert classify(DONE, _record(final, check=False)).verdict == "fail"


def test_every_run_refused_at_start_fails_the_void_gate_with_its_causes() -> None:
    """A systematic refusal (a broken price file refusing every run) never passes quietly."""
    price = {"status": "error", "code": "gate_blocked", "detail": "price_missing"}
    runs = [classify(DONE, _record(price, check=False, run=r)) for r in range(1, 7)]
    summary = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t")
    assert summary.gates["void"] is False
    assert summary.accepted is False
    # a price message is free text: its detail is `other`, its code kept (#429, #438)
    assert summary.report["void_causes"] == {"start_refused:gate_blocked:other": 6}


def test_a_hostile_detail_never_becomes_a_report_key() -> None:
    """#438 blocker 2: the detail comes from the child's raw line; only a closed one is kept."""
    hostile = {"status": "blocked", "code": "focus_lost", "detail": "Inbox (3) - you@example.com"}
    bad_code = {"status": "blocked", "code": "<script>", "detail": "page_hidden"}
    runs = [classify(DONE, _record(hostile, check=False, run=1)),
            classify(DONE, _record(bad_code, check=False, run=2)),
            classify(DONE, _record(PAGE_HIDDEN, check=False, run=3))]  # fmt: skip
    causes = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report["void_causes"]
    assert causes == {
        "start_refused:focus_lost:other": 1,
        "start_refused:other:page_hidden": 1,
        "start_refused:focus_lost:page_hidden": 1,
    }


def test_every_world_start_refusal_detail_is_a_closed_start_detail() -> None:
    """#438 blocker 2: each refusal `LiveWorld` can raise before the task (a literal message)
    is in `errors.START_DETAILS`, so the cause split keeps it; a new one fails here first."""
    import ast  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    from steerai.errors import START_DETAILS  # noqa: PLC0415
    from steerai.loop2 import live_world  # noqa: PLC0415

    raised = {
        str(node.args[1].value)
        for node in ast.walk(ast.parse(inspect.getsource(live_world)))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "SteerError"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
    }
    assert raised, "the scan found no refusal"
    assert raised <= START_DETAILS, sorted(raised - START_DETAILS)


def test_a_refused_start_with_no_detail_names_its_code_alone() -> None:
    got = classify(DONE, _record({"status": "blocked", "code": "focus_lost"}, check=False))
    assert (got.verdict, got.reason) == ("void", "start_refused:focus_lost")


def test_a_refused_start_is_not_also_a_cache_failure() -> None:
    runs = [_ok(DONE, 1, "void", final=PAGE_HIDDEN, reason="start_refused:focus_lost:page_hidden"),
            _ok(DONE, 2), _ok(DONE, 3), _ok(DONE, 4)]  # fmt: skip
    summary = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t")
    assert summary.gates["cache_zero"] is True
    assert summary.report["void_causes"] == {"start_refused:focus_lost:page_hidden": 1}


def test_a_refused_large_start_is_not_also_a_large_listed_failure() -> None:
    listed = _final(listings=[{"acted": 1, "acted_listed": True}])
    runs = [_ok(LARGE, 1, "void", final=PAGE_HIDDEN, reason="start_refused:focus_lost:page_hidden"),
            *(_ok(LARGE, r, final=listed) for r in (2, 3, 4))]  # fmt: skip
    summary = accept([LARGE], runs, controls=None, runs_per_task=3, label="t")
    assert summary.gates["large_listed"] is True


def test_the_report_counts_the_final_audit_by_result_and_reason() -> None:
    allowed = _final(audit={"ran": True, "result": "allowed", "reason": "closed"})
    vetoed = _final(audit={"ran": True, "result": "vetoed", "reason": "not_closed"})
    none = _final(audit={"ran": False, "result": None, "reason": None})
    runs = [_ok(DONE, 1, final=allowed), _ok(DONE, 2, final=vetoed), _ok(DONE, 3, final=none)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["audit"] == {
        "ran": 2,
        "result": {"allowed": 1, "vetoed": 1},
        "reason": {"closed": 1, "not_closed": 1},
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
    refused = _ok(DONE, 1, "void", final=PAGE_HIDDEN, reason="start_refused:focus_lost:page_hidden")
    ok = [_ok(DONE, r, final=_final(dispatch_risks=dict.fromkeys(RISKS, 0))) for r in (2, 3, 4)]
    summary = accept([DONE], [refused, *ok], controls=(5, 5), runs_per_task=3, label="t")
    assert summary.gates["no_r2_dispatch"] is True
    errored = _ok(DONE, 1, "fail", final={"status": "error", "detail": "internal_error"})
    refused_error = _ok(DONE, 1, "void", final={"status": "error", "detail": "price_missing"},
                        reason="start_refused:price_missing")  # fmt: skip
    summary = accept([DONE], [refused_error, *ok], controls=(5, 5), runs_per_task=3, label="t")
    assert summary.gates["no_r2_dispatch"] is True
    summary = accept([DONE], [errored, *ok], controls=(5, 5), runs_per_task=3, label="t")
    assert summary.gates["no_r2_dispatch"] is False


# ---- [V2.67] C: the child's stderr is kept in its run dir ---------------------------------------


def test_the_childs_stderr_reaches_its_file_only_through_dpapi(tmp_path: Path) -> None:
    """#438 blocker 1: the child's stderr can quote a title (a traceback's message, a list of
    windows); it is kept beside its final line only protected, never in plaintext."""
    from steerai.loop2.stage1 import STDERR_NAME, _start  # noqa: PLC0415

    title = "Secret Title - Inbox"
    protected: list[bytes] = []

    def protect(data: bytes) -> bytes:
        protected.append(data)
        return b"blob"

    class Popen:
        pid = 1

        def __init__(self, argv: list[str], **kw: Any) -> None:  # noqa: ARG002 - Any: Popen's keywords
            assert Path(kw["stdout"].name).name == "final.jsonl"
            assert kw["stderr"] is subprocess.PIPE
            self.stderr = io.BytesIO(f"ValueError: {title}\n".encode())

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - the Child protocol
            return 0

    child = _start(["steer"], tmp_path / "final.jsonl", popen=Popen, protect=protect)
    assert child.wait(timeout=1.0) == 0
    kept = (tmp_path / STDERR_NAME).read_bytes()
    assert title.encode() not in kept
    assert kept == base64.b64encode(b"blob") + b"\n"
    assert any(title.encode() in data for data in protected)


def test_a_stderr_that_cannot_be_protected_is_dropped_never_written_clear(tmp_path: Path) -> None:
    from steerai.loop2.stage1 import STDERR_NAME, STDERR_UNPROTECTED, _start  # noqa: PLC0415

    def protect(_data: bytes) -> bytes:
        raise OSError("no DPAPI")

    class Popen:
        pid = 1

        def __init__(self, argv: list[str], **kw: Any) -> None:  # noqa: ARG002 - Popen's shape
            self.stderr = io.BytesIO(b"Secret Title\n")

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - the Child protocol
            return 0

    _start(["steer"], tmp_path / "final.jsonl", popen=Popen, protect=protect).wait(timeout=1.0)
    assert (tmp_path / STDERR_NAME).read_bytes() == STDERR_UNPROTECTED
