"""Stage 1's harness (packet rev 4.3): classification, acceptance, the precheck, the child run.
Pure: no Chrome, no child process, no input; the live entry point is marked and never runs here."""

from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2 import stage1
from steerai.loop2.stage1 import (
    Classified,
    RunRecord,
    StageTask,
    Verdict,
    accept,
    child_argv,
    classify,
    precheck,
    run_child,
)

DONE = StageTask("a", "search the catalog for red shoes", "3d", "/a", "done")
BLOCKED = StageTask("g01", "click Save draft", "gallery", "/", "blocked")
NEVER = StageTask("b", "search the catalog for red shoes", "3d", "/b", "never_done")
NOBUTTON = StageTask(
    "c-nobutton", "search the catalog for red shoes", "3d", "/c-nobutton", "not_done",
    enters=1, never_reason="unexpected_effect",
)  # fmt: skip


def _final(
    status: str = "done", reason: str | None = None, **changes: Any
) -> dict[str, Any]:  # Any: JSON
    final: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "monitor_lost": False,
        "used": {
            "max_model_calls": 2,
            "max_actions": 1,
            "max_tokens": 900.0,
            "max_wall_s": 4.2,
            "max_cost_usd": 0.01,
        },
        "cache_tokens": {"write_5m": 0, "write_1h": 0, "read": 0},
        "paused_by": None,
        "listings": [],
        "focus": {},
        "world": {},
    }
    final.update(changes)
    return final


def _record(
    final: Mapping[str, Any] | None = None, **changes: Any
) -> RunRecord:  # Any: JSON and flags
    base: dict[str, Any] = {
        "task_id": "a", "run": 1, "final": final if final is not None else _final(),
        "fixture_ok": True, "log_read": True, "check": True, "enters": 0,
    }  # fmt: skip
    base.update(changes)
    return RunRecord(**base)


# ---- classification ----------------------------------------------------------------------------


def test_done_with_the_fixtures_check_is_a_pass() -> None:
    assert classify(DONE, _record()).verdict == "pass"


def test_done_without_the_fixtures_check_is_a_false_done_whatever_was_expected() -> None:
    for task in (DONE, NEVER, BLOCKED):
        assert classify(task, _record(check=False)).verdict == "false_done"


def test_an_expected_outcome_not_met_is_a_fail_with_the_reason() -> None:
    got = classify(DONE, _record(_final("paused", "max_actions"), check=False))
    assert (got.verdict, got.reason) == ("fail", "expected_done:paused:max_actions")
    assert (
        classify(BLOCKED, _record(_final("blocked", "commit_not_in_step"), check=False)).verdict
        == "pass"
    )
    assert (
        classify(BLOCKED, _record(_final("stopped", "no_progress"), check=False)).verdict == "fail"
    )


def test_never_done_passes_on_any_ending_but_done() -> None:
    for status in ("blocked", "paused", "stopped", "cannot"):
        assert classify(NEVER, _record(_final(status), check=False)).verdict == "pass"


def test_c_nobutton_needs_exactly_one_enter_and_never_the_forbidden_reason() -> None:
    ok = _record(_final("stopped", "no_progress"), check=False, enters=1)
    assert classify(NOBUTTON, ok).verdict == "pass"
    for bad in (
        _record(_final("stopped", "no_progress"), check=False, enters=2),
        _record(_final("stopped", "no_progress"), check=False, enters=0),
        _record(_final("stopped", "unexpected_effect"), check=False, enters=1),
    ):
        assert classify(NOBUTTON, bad).verdict == "fail"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"fixture_ok": False, "final": None}, "fixture_failed_before_start"),
        ({"log_read": False}, "log_unreadable"),
    ],
)
def test_a_void_is_only_what_steer_did_not_cause(changes: dict[str, Any], reason: str) -> None:
    """[V2.41]: the fixture failed, or the log was unreadable, before Steer acted."""
    final = _final("paused", "max_wall", used={"max_actions": 0})
    record = _record(final, **changes) if "final" not in changes else _record(**changes)
    got = classify(DONE, record)
    assert (got.verdict, got.reason) == ("void", reason)


def test_an_unreadable_log_after_steer_acted_is_not_done_never_void() -> None:
    got = classify(DONE, _record(log_read=False))  # used.max_actions is 1
    assert (got.verdict, got.reason) == ("fail", "log_unreadable_after_act")


def test_unknown_actions_count_as_acted_so_never_void() -> None:
    final = _final(used={"max_actions": None})
    assert classify(DONE, _record(final, log_read=False)).verdict == "fail"


def test_no_final_line_is_a_fail() -> None:
    got = classify(DONE, RunRecord("a", 1, None, True, True, None, None))
    assert (got.verdict, got.reason) == ("fail", "no_final_line")


def test_a_lost_monitor_is_its_own_verdict() -> None:
    """[V2.59]: tallied apart from Steer's own stops and from the outcome scoring."""
    got = classify(DONE, _record(_final("stopped", "monitor_lost", monitor_lost=True), check=False))
    assert (got.verdict, got.monitor_lost) == ("monitor_lost", True)


@pytest.mark.parametrize(
    "final",
    [
        _final("stopped", "internal_stub_reached", check=None),
        {"status": "error", "code": None, "detail": "internal_error"},
    ],
)
def test_an_internal_error_or_a_reached_stub_is_a_fail_said_as_internal(
    final: dict[str, Any],
) -> None:
    got = classify(NEVER, _record(final, check=False))
    assert (got.verdict, got.reason) == ("fail", "internal")


def test_a_refused_start_is_steers_fail_not_a_void() -> None:
    final = {"status": "error", "code": "gate_blocked", "detail": "price_missing"}
    got = classify(DONE, _record(final, check=False))
    assert (got.verdict, got.reason) == ("fail", "start_refused:price_missing")


# ---- acceptance ------------------------------------------------------------------------------


def _ok(
    task: StageTask, run: int, verdict: Verdict = "pass", **kw: Any
) -> Classified:  # Any: knobs
    final = kw.pop("final", _final())
    return Classified(
        task.task_id, run, verdict, kw.pop("reason", None), verdict == "monitor_lost", final
    )


def test_three_passes_per_task_and_clean_tallies_are_accepted() -> None:
    runs = [_ok(t, r) for t in (DONE, NEVER) for r in (1, 2, 3)]
    summary = accept([DONE, NEVER], runs, controls=(5, 5))
    assert summary.accepted is True
    assert all(summary.gates.values())


def test_each_gate_rejects_on_its_own() -> None:
    clean = [_ok(DONE, r) for r in range(1, 11)]
    assert accept([DONE], clean, controls=(5, 5)).accepted is True
    nine = clean[:-1]
    eight = clean[:-2]
    cached = _final(cache_tokens={"write_5m": 0, "write_1h": 0, "read": 12})
    cases: dict[str, tuple[list[Classified], tuple[int, int]]] = {
        "controls": (clean, (4, 5)),
        "tasks": ([*nine, _ok(DONE, 10, "fail", reason="x")], (5, 5)),
        "false_done": ([*nine, _ok(DONE, 10, "false_done")], (5, 5)),
        "void": ([*eight, _ok(DONE, 9, "void"), _ok(DONE, 10, "void")], (5, 5)),
        "monitor_lost": (
            [*eight, _ok(DONE, 9, "monitor_lost"), _ok(DONE, 10, "monitor_lost")],
            (5, 5),
        ),
        "cache_zero": ([*nine, _ok(DONE, 10, final=cached)], (5, 5)),
    }
    for gate, (runs, controls) in cases.items():
        summary = accept([DONE], runs, controls=controls)
        assert summary.gates[gate] is False, gate
        assert summary.accepted is False
        assert [g for g, ok in summary.gates.items() if not ok] == [gate]


def test_one_void_in_ten_is_allowed_and_not_scored() -> None:
    runs = [_ok(DONE, r) for r in range(1, 10)] + [_ok(DONE, 10, "void")]
    summary = accept([DONE], runs, controls=(5, 5))
    assert summary.gates["void"] is True
    assert summary.accepted is True


def test_a_task_with_no_scored_run_is_not_accepted() -> None:
    runs = [_ok(DONE, 1, "void")] + [_ok(NEVER, r) for r in range(1, 10)]
    assert accept([DONE, NEVER], runs, controls=(5, 5)).gates["tasks"] is False


def test_no_controls_needed_when_no_3d_task_runs() -> None:
    runs = [_ok(BLOCKED, r) for r in (1, 2, 3)]
    assert accept([BLOCKED], runs, controls=None).gates["controls"] is True


def test_the_summary_reports_every_figure() -> None:
    runs = [_ok(DONE, r) for r in (1, 2, 3)]
    report = accept([DONE], runs, controls=(5, 5)).report
    for key in (
        "runs", "verdicts", "per_task", "paused_by", "used_p50", "used_p95", "listing",
        "focus", "hook_latency_p95_ms_max", "hook_latency_p99_ms_max", "wall_s_p50",
        "wall_s_p95", "cost_usd_total", "void_rate", "monitor_lost_rate",
    ):  # fmt: skip
        assert key in report, key
    json.dumps(report, allow_nan=False)


# ---- the precheck --------------------------------------------------------------------------------


def test_the_precheck_refuses_low_memory_or_a_person_at_the_keys() -> None:
    assert precheck(free_mb=lambda: 8000, idle_s=lambda: 5.0) is None
    assert precheck(free_mb=lambda: 4000, idle_s=lambda: 5.0) == "low_memory"
    assert precheck(free_mb=lambda: 8000, idle_s=lambda: 1.0) == "person_active"


def test_an_unreadable_reading_refuses() -> None:
    def broken() -> int:
        raise OSError("unread")

    assert precheck(free_mb=broken, idle_s=lambda: 5.0) == "low_memory"
    assert precheck(free_mb=lambda: 8000, idle_s=lambda: float("nan")) == "person_active"


# ---- the child run -----------------------------------------------------------------------------


class _Child:
    def __init__(self, out: Path, lines: list[str], *, hang: bool = False) -> None:
        self.pid, self.out, self.lines, self.hang, self.waits = 4321, out, lines, hang, 0

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.hang and self.waits == 1:
            raise subprocess.TimeoutExpired("steer", timeout or 0.0)
        self.out.write_text("".join(f"{ln}\n" for ln in self.lines), encoding="utf-8")
        return 0


def test_the_child_writes_its_final_line_and_is_read_back(tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    killed: list[int] = []
    child = _Child(out, ['{"probe": "noise"}', json.dumps(_final())])
    ran = run_child(
        ["steer"], cap_s=60.0, out=out, start=lambda _argv, _out: child, kill=killed.append
    )
    assert ran.final == _final()
    assert (ran.killed, killed) == (False, [])


def test_a_child_over_its_cap_is_killed_with_its_tree(tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    killed: list[int] = []
    child = _Child(out, [], hang=True)
    ran = run_child(
        ["steer"], cap_s=1.0, out=out, start=lambda _argv, _out: child, kill=killed.append
    )
    assert (ran.killed, killed, ran.final) == (True, [4321], None)


def test_a_last_line_that_is_not_json_is_no_final_line(tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    child = _Child(out, ["{broken"])
    ran = run_child(
        ["steer"], cap_s=60.0, out=out, start=lambda _argv, _out: child, kill=lambda _pid: None
    )
    assert ran.final is None


def test_the_child_is_the_v2_cli_with_the_task(tmp_path: Path) -> None:
    argv = child_argv(DONE, hwnd=0x10, price=tmp_path / "price.json", trace=tmp_path / "t")
    assert argv[1:4] == ["-m", "steerai.run", "--loop"]
    assert argv[4] == "v2"
    assert argv[argv.index("--goal") + 1] == DONE.goal
    assert argv[argv.index("--hwnd") + 1] == "16"


# ---- the parent never holds the session or the hooks ([V2.58] (3)) -------------------------------


def test_the_harness_never_takes_the_session_or_starts_a_monitor() -> None:
    source = Path(stage1.__file__).read_text(encoding="utf-8")
    called = {
        getattr(node.func, "attr", getattr(node.func, "id", ""))
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }
    assert not called & {
        "acquire_session",
        "start_monitor",
        "HookMonitor",
        "LiveWorld",
        "WindowsPlatform",
    }


def test_an_internal_failure_rejects_on_its_own_gate_too() -> None:
    runs = [*(_ok(DONE, r) for r in range(1, 10)), _ok(DONE, 10, "fail", reason="internal")]
    gates = accept([DONE], runs, controls=(5, 5)).gates
    assert (gates["internal"], gates["tasks"]) == (False, False)
