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

from steerai.loop2 import cli, stage1
from steerai.loop2.loop import Reason, TaskOutcome
from steerai.loop2.stage1 import (
    MAX_RERUNS,
    THREE_D,
    Classified,
    RunRecord,
    StageTask,
    Verdict,
    accept,
    child_argv,
    classify,
    precheck,
    run_child,
    run_task_runs,
    wait_for_exit,
    wait_until_ready,
)
from steerai.loop2.task import Budgets

DONE = StageTask("a", "search the catalog for red shoes", "3d", "/a", "done")
BLOCKED = StageTask("g01", "click Save draft", "gallery", "/", "blocked")
NEVER = StageTask("b", "search the catalog for red shoes", "3d", "/b", "never_done")
NOBUTTON = StageTask(
    "c-nobutton", "search the catalog for red shoes", "3d", "/c-nobutton", "not_done",
    enters=1, never_reason="unexpected_effect",
)  # fmt: skip


BUDGETS = Budgets(max_actions=40, max_model_calls=20, max_tokens=400_000, max_cost_usd=2.0,
                  max_wall_s=300.0)  # fmt: skip


def _final(
    status: str = "done", reason: str | None = None, **changes: Any
) -> dict[str, Any]:  # Any: JSON
    """A final line as the CLI writes it (`cli._result`, #366 P3: the contract is pinned), with
    the fields a test needs changed."""
    outcome = TaskOutcome(
        status=status,  # type: ignore[arg-type]  # a status token under test
        cause=None if reason is None else Reason(reason),
        dispatched=(),
        model_calls=2,
        strikes=(),
        tokens=900.0,
        cost_usd=0.01,
        wall_s=4.2,
    )
    report: dict[str, int | str | None] = {"monitor_lost": 0}
    final = json.loads(json.dumps(cli._result(outcome, BUDGETS, report), allow_nan=False))
    final["used"]["max_actions"] = 1  # one dispatch, unless a test says otherwise
    final.update(changes)
    return dict(final)


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
    got = classify(DONE, _record(_final("stopped", "no_progress"), log_read=False, check=None))
    assert (got.verdict, got.reason) == ("fail", "log_unreadable_after_act")


def test_unknown_actions_count_as_acted_so_never_void() -> None:
    final = _final("stopped", "no_progress", used={"max_actions": None})
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
    summary = accept([DONE, NEVER], runs, controls=(5, 5), runs_per_task=3, label="t")
    assert summary.accepted is True
    assert all(summary.gates.values())


def test_each_gate_rejects_on_its_own() -> None:
    clean = [_ok(DONE, r) for r in range(1, 11)]
    assert accept([DONE], clean, controls=(5, 5), runs_per_task=10, label="t").accepted is True
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
        passes = sum(r.verdict in ("pass", "fail") for r in runs)  # the scored runs
        summary = accept([DONE], runs, controls=controls, runs_per_task=passes, label="t")
        assert summary.gates[gate] is False, gate
        assert summary.accepted is False
        assert [g for g, ok in summary.gates.items() if not ok] == [gate]


def test_one_void_in_ten_is_allowed_and_not_scored() -> None:
    runs = [_ok(DONE, r) for r in range(1, 10)] + [_ok(DONE, 10, "void")]
    summary = accept([DONE], runs, controls=(5, 5), runs_per_task=9, label="t")
    assert summary.gates["void"] is True
    assert summary.accepted is True


def test_a_task_with_no_scored_run_is_not_accepted() -> None:
    runs = [_ok(DONE, 1, "void")] + [_ok(NEVER, r) for r in range(1, 10)]
    assert (
        accept([DONE, NEVER], runs, controls=(5, 5), runs_per_task=9, label="t").gates["tasks"]
        is False
    )


def test_no_controls_needed_when_no_3d_task_runs() -> None:
    runs = [_ok(BLOCKED, r) for r in (1, 2, 3)]
    assert (
        accept([BLOCKED], runs, controls=None, runs_per_task=3, label="t").gates["controls"] is True
    )


def test_the_summary_reports_every_figure() -> None:
    runs = [_ok(DONE, r) for r in (1, 2, 3)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
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
    gates = accept([DONE], runs, controls=(5, 5), runs_per_task=10, label="t").gates
    assert (gates["internal"], gates["tasks"]) == (False, False)


# ---- #366 P2: the order of the rules ---------------------------------------------------------


def test_done_with_a_lost_monitor_and_a_failed_check_is_false_done() -> None:
    final = _final("done", monitor_lost=True)
    assert classify(DONE, _record(final, check=False)).verdict == "false_done"


def test_done_with_an_unread_log_is_a_fail_never_a_void() -> None:
    final = _final("done", used={"max_actions": 0})
    got = classify(DONE, _record(final, log_read=False, check=None))
    assert (got.verdict, got.reason) == ("fail", "done_log_unread")


def test_the_enter_count_is_checked_before_a_lost_monitor_when_the_log_was_read() -> None:
    final = _final("stopped", "monitor_lost", monitor_lost=True)
    got = classify(NOBUTTON, _record(final, check=False, enters=2))
    assert (got.verdict, got.reason) == ("fail", "enters:2")


def test_a_lost_monitor_comes_before_the_unread_log_void() -> None:
    final = _final("stopped", "monitor_lost", monitor_lost=True, used={"max_actions": 0})
    assert classify(DONE, _record(final, log_read=False, check=None)).verdict == "monitor_lost"


def test_internal_comes_before_a_refused_start_and_a_lost_monitor() -> None:
    final = {"status": "error", "code": None, "detail": "internal_error", "monitor_lost": True}
    assert classify(DONE, _record(final, check=None)).reason == "internal"


def test_a_crash_followed_by_a_noise_line_is_no_final_line(tmp_path: Path) -> None:
    """#366 E2: the last JSON line must be the CLI's own, with a known status."""
    out = tmp_path / "out.jsonl"
    child = _Child(out, ["Traceback (most recent call last):", '{"probe": "noise"}'])
    ran = run_child(
        ["steer"], cap_s=60.0, out=out, start=lambda _argv, _out: child, kill=lambda _pid: None
    )
    got = classify(DONE, _record(ran.final))
    assert (got.verdict, got.reason) == ("fail", "no_final_line")
    assert classify(DONE, _record({"status": "finished"})).reason == "no_final_line"


def test_an_expected_reason_must_be_the_runs_own() -> None:
    """#366 P4 (E7): c-nobutton ends through the no-progress guard (runner: paused /
    no_progress); a gallery row expected blocked must be blocked by the step-3 commit rule."""
    nobutton = classify(THREE_D[3], _record(_final("paused", "no_progress"), check=False, enters=1))
    assert nobutton.verdict == "pass"
    other = classify(THREE_D[3], _record(_final("cannot", "page_not_ready"), check=False, enters=1))
    assert (other.verdict, other.reason) == ("fail", "reason:page_not_ready")
    blocked = StageTask("g01", "click Save draft", "gallery", "/", "blocked",
                        reason="commit_not_in_step")  # fmt: skip
    assert (
        classify(blocked, _record(_final("blocked", "commit_not_in_step"), check=False)).verdict
        == "pass"
    )
    assert (
        classify(blocked, _record(_final("blocked", "gate_blocked"), check=False)).verdict == "fail"
    )


# ---- #366 P1: 3 of 3, re-runs, and a refused precheck is said -----------------------------------


def test_a_task_passes_only_with_as_many_passes_as_runs_per_task() -> None:
    runs = [_ok(DONE, 1, "void"), _ok(DONE, 2, "void"), _ok(DONE, 3)]
    assert accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").gates["tasks"] is False
    rerun = [*runs, _ok(DONE, 4), _ok(DONE, 5)]  # the voids re-run
    assert accept([DONE], rerun, controls=(5, 5), runs_per_task=3, label="t").gates["tasks"] is True


def test_fewer_than_three_runs_per_task_is_never_acceptance() -> None:
    with pytest.raises(ValueError, match="3"):
        accept([DONE], [_ok(DONE, 1)], controls=(5, 5), runs_per_task=1, label="t")


def test_a_refused_precheck_is_counted_and_counts_in_the_void_rate() -> None:
    runs = [_ok(DONE, r) for r in range(1, 10)]
    summary = accept([DONE], runs, controls=(5, 5), runs_per_task=9, not_started=2, label="t")
    assert summary.report["not_started"] == 2
    assert summary.gates["void"] is False  # 2 of 11 attempts were not Steer's: over 1 in 10


def test_the_summary_says_which_set_it_is() -> None:
    runs = [_ok(DONE, r) for r in (1, 2, 3)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="3d").report
    assert report["set"] == "3d"


def test_a_large_page_task_fails_closed_until_listed_when_acted_is_checked() -> None:
    """#366 P5: acceptance needs the target listed on the turn it is acted on; until that
    check exists, a set with a `/large.html` task is never accepted."""
    large = StageTask("large-reserve", "reserve item 1987", "large", "/large.html", "done")
    runs = [_ok(large, r) for r in (1, 2, 3)]
    summary = accept([large], runs, controls=None, runs_per_task=3, label="large")
    assert (summary.gates["large_listed"], summary.accepted) == (False, False)
    other = accept([DONE], [_ok(DONE, r) for r in (1, 2, 3)], controls=(5, 5), runs_per_task=3,
                   label="t")  # fmt: skip
    assert other.gates["large_listed"] is True


def test_odd_values_in_a_final_never_crash_the_summary_and_are_said() -> None:
    """#366 E10: NaN, None or a string in the listings or the use is incomplete, never a crash
    and never a NaN in the summary."""
    odd = [{"eligible": None, "shown": 1, "cut": "x", "keep_at_ceiling": False,
            "goal_at_subcap": False, "prompt_cut": float("nan"), "prompt_tokens": "x"}]  # fmt: skip
    final = _final(listings=odd, used={"max_actions": 1, "max_wall_s": float("nan")})
    runs = [_ok(DONE, 1, final=final), _ok(DONE, 2), _ok(DONE, 3)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    json.dumps(report, allow_nan=False)
    assert report["listing"]["incomplete"] == 1


# ---- #366 small fixes ---------------------------------------------------------------------------


def test_a_capped_run_reads_cap_killed() -> None:
    record = RunRecord("a", 1, None, True, False, None, None, killed=True, code=None)
    got = classify(DONE, record)
    assert (got.verdict, got.reason) == ("fail", "cap_killed")


def test_the_wait_after_a_kill_is_bounded(tmp_path: Path) -> None:
    class Stuck(_Child):
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("steer", timeout or 0.0)

    out = tmp_path / "out.jsonl"
    killed: list[int] = []
    child = Stuck(out, [])
    ran = run_child(["steer"], cap_s=1.0, out=out, start=lambda _a, _o: child, kill=killed.append)
    assert (ran.killed, ran.code, killed) == (True, None, [4321])


def test_the_harness_imports_nothing_that_holds_the_session_or_the_hooks() -> None:
    source = Path(stage1.__file__).read_text(encoding="utf-8")
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    banned = ("steerai.session", "steerai.loop2.live_world", "steerai.platform", "monitor")
    assert not [m for m in modules if m.startswith(banned) or m.endswith("monitor")]


# ---- mutants that survived the first run --------------------------------------------------------


def test_never_done_fails_when_steer_says_done_and_the_page_agrees() -> None:
    got = classify(NEVER, _record(_final("done"), check=True))
    assert (got.verdict, got.reason) == ("fail", "expected_never_done:done:None")


def test_a_final_without_cache_counters_fails_the_cache_gate() -> None:
    bare = _final()
    del bare["cache_tokens"]
    runs = [_ok(DONE, 1, final=bare), _ok(DONE, 2), _ok(DONE, 3)]
    assert (
        accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").gates["cache_zero"]
        is False
    )


def test_the_idle_floor_is_exactly_two_seconds() -> None:
    assert precheck(free_mb=lambda: 8000, idle_s=lambda: 2.0) is None
    assert precheck(free_mb=lambda: 8000, idle_s=lambda: 1.99) == "person_active"
    assert precheck(free_mb=lambda: 4096, idle_s=lambda: 5.0) is None
    assert precheck(free_mb=lambda: 4095, idle_s=lambda: 5.0) == "low_memory"


# ---- #366 re-review: a world that refused to start is never an outcome --------------------------


def test_a_world_start_refusal_on_a_never_done_task_is_a_fail_not_a_pass() -> None:
    """cli.py's shape when LiveWorld refused to start (the session still held by a killed
    tree, the monitor, the window): no reason, no use, no cache counters."""
    refusal = {"task_id": "t", "status": "blocked", "code": "gate_blocked",
               "detail": "steer_already_running"}  # fmt: skip
    got = classify(NEVER, _record(refusal, check=False))
    assert (got.verdict, got.reason) == ("fail", "start_refused:gate_blocked")


# ---- #366 re-review: the re-run loop and the wait for the person, pure -------------------------


def _scripted(*outcomes: str) -> tuple[list[int], Any]:  # Any: the attempt callable
    """An attempt per run: a verdict-producing record, or a not-started reason."""
    asked: list[int] = []
    script = list(outcomes)

    def attempt(run: int) -> RunRecord | str:
        asked.append(run)
        kind = script.pop(0)
        if kind in ("low_memory", "person_active"):
            return kind
        if kind == "void":
            return RunRecord("a", run, None, False, False, None, None)
        if kind == "lost":
            return _record(
                _final("stopped", "monitor_lost", monitor_lost=True), check=False, run=run
            )
        if kind == "fail":
            return _record(_final("paused", "max_actions"), check=False, run=run)
        return _record(run=run)

    return asked, attempt


def test_only_pass_fail_and_false_done_are_scored_runs() -> None:
    asked, attempt = _scripted("void", "pass", "lost", "fail", "pass")
    runs, not_started = run_task_runs(DONE, runs=3, attempt=attempt)
    assert [r.verdict for r in runs] == ["void", "pass", "monitor_lost", "fail", "pass"]
    assert (asked, not_started) == ([1, 2, 3, 4, 5], 0)


def test_re_runs_stop_at_the_bound_and_the_task_fails_closed() -> None:
    asked, attempt = _scripted(*["void"] * 10)
    runs, _ = run_task_runs(DONE, runs=3, attempt=attempt)
    assert len(asked) == 3 + MAX_RERUNS
    summary = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t")
    assert summary.gates["tasks"] is False


def test_a_run_not_started_is_counted_and_re_run() -> None:
    asked, attempt = _scripted("low_memory", "pass", "pass", "pass")
    runs, not_started = run_task_runs(DONE, runs=3, attempt=attempt)
    assert (len(runs), not_started, asked) == (3, 1, [1, 2, 3, 4])


def test_the_harness_waits_for_the_person_before_a_run_is_not_started() -> None:
    clock = {"t": 0.0}
    readings = iter(["person_active", "person_active", None])
    got = wait_until_ready(
        lambda: next(readings), sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        clock=lambda: clock["t"],
    )  # fmt: skip
    assert got is None
    assert 0 < clock["t"] < stage1.IDLE_WAIT_S


def test_a_person_still_at_the_keys_after_the_bound_refuses() -> None:
    clock = {"t": 0.0}
    got = wait_until_ready(
        lambda: "person_active", sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        clock=lambda: clock["t"],
    )  # fmt: skip
    assert got == "person_active"
    assert clock["t"] >= stage1.IDLE_WAIT_S


# ---- low memory is waited out like the person (the 3d shakedown's 20 low_memory refusals) -------


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def test_memory_that_recovers_inside_the_bound_starts_the_run() -> None:
    clock = _Clock()
    readings = iter(["low_memory", "low_memory", "low_memory", None])
    got = wait_until_ready(lambda: next(readings), sleep=clock.sleep, clock=lambda: clock.t)
    assert got is None
    assert clock.slept == [stage1.MEMORY_POLL_S] * 3


def test_memory_still_low_past_its_bound_is_a_run_not_started() -> None:
    clock = _Clock()
    got = wait_until_ready(lambda: "low_memory", sleep=clock.sleep, clock=lambda: clock.t)
    assert got == "low_memory"
    assert stage1.MEMORY_WAIT_S <= clock.t < stage1.MEMORY_WAIT_S + stage1.MEMORY_POLL_S + 1e-9


def test_the_floor_stays_the_users_rule() -> None:
    assert stage1.FLOOR_MB == 4096
    assert (stage1.MEMORY_WAIT_S, stage1.MEMORY_POLL_S) == (60.0, 2.0)


def test_a_kill_during_the_memory_wait_ends_it_and_is_never_a_run_not_started() -> None:
    checks: list[int] = []

    def check() -> str:
        checks.append(1)
        return "low_memory"

    def killed(_s: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        wait_until_ready(check, sleep=killed, clock=lambda: 0.0)
    assert checks == [1]


def test_an_unknown_refusal_is_not_waited_for() -> None:
    clock = _Clock()
    assert wait_until_ready(lambda: "other", sleep=clock.sleep, clock=lambda: clock.t) == "other"
    assert clock.slept == []


# ---- the controls' own Chrome processes exit before the first task ------------------------------


def test_the_first_task_waits_until_the_controls_processes_have_exited() -> None:
    clock = _Clock()
    alive = iter([{11, 12, 99}, {12, 99}, {99}])
    left = wait_for_exit({11, 12}, alive=lambda: next(alive), sleep=clock.sleep,
                         clock=lambda: clock.t)  # fmt: skip
    assert left == set()
    assert clock.slept == [stage1.EXIT_POLL_S] * 2


def test_processes_still_alive_past_the_bound_are_returned_never_killed() -> None:
    clock = _Clock()
    left = wait_for_exit({11}, alive=lambda: {11}, sleep=clock.sleep, clock=lambda: clock.t)
    assert left == {11}
    assert clock.t >= stage1.EXIT_WAIT_S


def test_a_kill_during_the_exit_wait_ends_it() -> None:
    def killed(_s: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        wait_for_exit({11}, alive=lambda: {11}, sleep=killed, clock=lambda: 0.0)


def test_the_controls_record_their_chrome_tree_before_it_is_stopped() -> None:
    """The live wiring, pinned: `run_control` hands the harness its Chrome's pid before
    `stop_chrome`, and `main` waits for the recorded tree before the first task."""
    source = Path(stage1.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    names = {n.func.id for n in ast.walk(main) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)}  # fmt: skip
    assert "wait_for_exit" in names
    from steerai.probes import instant_search  # noqa: PLC0415 - the probe's own source

    probe = ast.parse(Path(instant_search.__file__).read_text(encoding="utf-8"))
    control = next(n for n in probe.body
                   if isinstance(n, ast.FunctionDef) and n.name == "run_control")  # fmt: skip
    assert "on_stop" in {a.arg for a in control.args.kwonlyargs}


def test_an_unfinished_runs_unknown_cache_counters_are_not_a_cache_failure() -> None:
    """#366: a port-error stop already fails; its counters are unknown (null), never a second
    failure under cache_zero. A final with no counters at all still fails the gate."""
    unknown = _final(cache_tokens={"write_5m": None, "write_1h": None, "read": None})
    runs = [_ok(DONE, 1, final=unknown), _ok(DONE, 2), _ok(DONE, 3)]
    gates = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").gates
    assert gates["cache_zero"] is True


def test_the_summary_names_the_runs_that_failed_each_gate() -> None:
    cached = _final(cache_tokens={"write_5m": 0, "write_1h": 0, "read": 12})
    runs = [_ok(DONE, 1), _ok(DONE, 2, "false_done"), _ok(DONE, 3, final=cached),
            _ok(DONE, 4, "fail", reason="internal")]  # fmt: skip
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["gate_failures"] == {
        "false_done": ["a#2"], "cache_zero": ["a#3"], "internal": ["a#4"], "tasks": ["a"],
    }  # fmt: skip


def test_the_summary_sums_the_watch_deadline_counts_per_class() -> None:
    """[V2.48]: stage 1 reports per class the watches that ended at their deadline."""
    one = _final(watch_deadlines={"trigger": {"watches": 2, "deadline": 1},
                                  "wait": {"watches": 1, "deadline": 0}})  # fmt: skip
    two = _final(watch_deadlines={"trigger": {"watches": 3, "deadline": 2},
                                  "submit": {"watches": 1, "deadline": "x"}})  # fmt: skip
    runs = [_ok(DONE, 1, final=one), _ok(DONE, 2, final=two), _ok(DONE, 3)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["watch_deadlines"] == {
        "plain": {"watches": 0, "deadline": 0},
        "trigger": {"watches": 5, "deadline": 3},
        "submit": {"watches": 1, "deadline": 0},
        "wait": {"watches": 1, "deadline": 0},
    }
    assert report["watch_deadlines_incomplete"] == 1  # the odd count was said, not summed
