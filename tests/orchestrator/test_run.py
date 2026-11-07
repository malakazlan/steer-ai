"""The task runner: a plan file, a window, a console and an exit code (RN-02/11/15/21/23/27/28/
29/34/35/36/37/39/40/46/18)."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import Plan, Step, StepResult, Subgoal
from steerai.orchestrator.ledger import LedgerError
from steerai.orchestrator.loop import TaskResult
from steerai.orchestrator.plan_io import plan_to_json
from steerai.probes.windows import WindowInfo
from steerai.run import (
    EXIT_BLOCKED,
    EXIT_DONE,
    EXIT_ERROR,
    EXIT_PAUSED,
    EXIT_STOPPED,
    EXIT_USAGE,
    Bound,
    Deps,
    Options,
    RunnerError,
    SafeTrace,
    UsageError,
    over_limit,
    parse_args,
    resolve_window,
    run,
    warm_up,
)
from steerai.verify.predicates import Appeared, Within
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import Row, build
from tests.orchestrator.test_loop import _click, _send, _type

CHROME = WindowInfo(hwnd=0x10, title="Amir Khan - Chat - Google Chrome", pid=500)
NOTEPAD = WindowInfo(hwnd=0x20, title="Untitled - Notepad", pid=600)
CONSOLE = WindowInfo(hwnd=0x30, title="python -m steerai.run --window Chrome", pid=700)


def _plan(*steps: Step) -> Plan:
    """A plan with a file form: the condition names a landmark, never an anchor id."""
    condition = Appeared(
        role="Text", name_pattern="hello there", within=Within(landmark="Conversation")
    )
    return Plan(
        goal="send hello there to Amir",
        subgoals=(Subgoal(id="g1", desc="sent", condition=condition),),
        steps=tuple(replace(s, expect=None) for s in steps),  # templates fill ids per step
    )


# ---- pure parts --------------------------------------------------------------------------


def _id(step: Step | None) -> str | None:
    return None if step is None else step.id


def test_the_limit_is_on_the_effective_risk_over_the_whole_plan() -> None:
    delete_click = _click(step_id="d", name="Delete chat", risk="R1")
    plan = _plan(_type(), delete_click, _send())
    assert over_limit(plan, "R3") is None
    assert _id(over_limit(plan, "R2")) == delete_click.id
    assert _id(over_limit(plan, "R1")) == delete_click.id
    assert over_limit(_plan(_type(), _send()), "R1") is not None
    assert over_limit(_plan(_type()), "R1") is None
    assert over_limit(_plan(_type()), "R0") is not None


def test_resolve_window_needs_exactly_one_match_outside_the_exclusions() -> None:
    windows = [CHROME, NOTEPAD, CONSOLE]
    with pytest.raises(RunnerError):
        resolve_window(windows, "chrome", excluded_hwnds=set(), excluded_pids=set())
    assert (
        resolve_window(windows, "chrome", excluded_hwnds={CONSOLE.hwnd}, excluded_pids=set())
        == CHROME
    )
    assert (
        resolve_window(windows, "CHROME", excluded_hwnds=set(), excluded_pids={CONSOLE.pid})
        == CHROME
    )


def test_resolve_window_reports_the_candidates_on_ambiguity_and_none_on_a_miss() -> None:
    with pytest.raises(RunnerError) as ambiguous:
        resolve_window([CHROME, CONSOLE], "chrome", excluded_hwnds=set(), excluded_pids=set())
    assert ambiguous.value.code is ErrorCode.ELEMENT_NOT_FOUND
    assert ambiguous.value.detail == "ambiguous_target"
    assert [c.hwnd for c in ambiguous.value.candidates] == [CHROME.hwnd, CONSOLE.hwnd]
    with pytest.raises(RunnerError) as missing:
        resolve_window([NOTEPAD], "chrome", excluded_hwnds=set(), excluded_pids=set())
    assert missing.value.detail == "no_match"


def test_resolve_window_counts_an_unreadable_twin_and_never_picks_an_unreadable_window() -> None:
    """Issue #48 (CU-05/24): a window DWM cannot read stays a candidate for ambiguity, but is
    never the one picked."""
    twin = WindowInfo(hwnd=0x41, title=CHROME.title, pid=CHROME.pid, cloak_unreadable=True)
    with pytest.raises(RunnerError) as ambiguous:
        resolve_window([CHROME, twin], "chrome", excluded_hwnds=set(), excluded_pids=set())
    assert ambiguous.value.detail == "ambiguous_target"
    with pytest.raises(RunnerError) as unclear:
        resolve_window([twin, NOTEPAD], "chrome", excluded_hwnds=set(), excluded_pids=set())
    assert (unclear.value.code, unclear.value.detail) == (
        ErrorCode.WINDOW_OTHER_DESKTOP,
        "cloak_unreadable",
    )


def test_resolve_window_compares_normalised_casefolded_titles() -> None:
    title = chr(0xFF21) + "mir Khan " + chr(0x2013) + " Chat"
    fullwidth = WindowInfo(hwnd=0x40, title=title, pid=800)
    found = resolve_window([fullwidth], "amir khan", excluded_hwnds=set(), excluded_pids=set())
    assert found == fullwidth


def test_resolve_window_refuses_a_minimized_match() -> None:
    minimized = WindowInfo(hwnd=0x50, title="Amir - Chat", pid=900, minimized=True)
    with pytest.raises(RunnerError) as info:
        resolve_window([minimized], "amir", excluded_hwnds=set(), excluded_pids=set())
    assert info.value.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert info.value.detail == "minimized"


@pytest.mark.parametrize("needle", ["", "   "])
def test_an_empty_window_substring_is_a_usage_error(needle: str) -> None:
    with pytest.raises(UsageError):
        resolve_window([CHROME], needle, excluded_hwnds=set(), excluded_pids=set())


def test_warm_up_waits_for_three_equal_node_counts_or_the_cap() -> None:
    counts = iter([3, 40, 55, 60, 60, 60, 61])
    clock = {"t": 0.0}

    def observe() -> Snapshot:
        rows: list[Row] = [(i, "Group", f"n{i}", (42, i), None) for i in range(next(counts))]
        return build(rows, snapshot_id="s", taken_at=clock["t"])

    def sleep(s: float) -> None:
        clock["t"] += s

    settled = warm_up(observe, cap_s=10.0, monotonic=lambda: clock["t"], sleep=sleep)
    assert settled.node_count == 60
    capped = warm_up(
        lambda: build([(0, "Pane", "p", (42, 1), None)], snapshot_id="s", taken_at=0.0),
        cap_s=0.0,
        monotonic=lambda: clock["t"],
        sleep=sleep,
    )
    assert capped.node_count == 1


def test_parse_args_validates_numbers_and_maps_usage_errors_to_exit_1(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    options = parse_args(["--plan", str(plan), "--window", "Chrome", "--max-risk", "R1"])
    assert options.max_risk == "R1"
    assert options.task_id, "a task id is always present"
    for bad in (
        ["--plan", str(plan), "--window", "Chrome", "--max-steps", "-5"],
        ["--plan", str(plan), "--window", "Chrome", "--verify-deadline-s", "-1"],
        ["--plan", str(plan), "--window", "Chrome", "--hwnd", "16"],
        ["--plan", str(plan), "--hwnd", "0"],
        ["--plan", str(plan), "--hwnd", "-1"],
        ["--plan", str(plan)],
        ["--bogus"],
        [],
    ):
        with pytest.raises(UsageError):
            parse_args(bad)
    assert parse_args(["--plan", str(plan), "--hwnd", "0x10"]).hwnd == 0x10


def test_a_trace_failure_after_the_first_write_is_a_warning_not_a_crash() -> None:
    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def write(self, _record: Any) -> None:
            self.calls += 1
            if self.calls > 1:
                raise OSError("disk full")

    stderr = io.StringIO()
    trace = SafeTrace(Flaky(), stderr)
    trace.write({"kind": "task"})
    trace.write({"kind": "step"})
    trace.write({"kind": "step"})
    assert stderr.getvalue().count("trace") == 1, "warned once"


# ---- run: assembly, order and exit codes ----------------------------------------------------


@dataclass
class FakeLedger:
    task_id: str
    plan_digests: list[str] = field(default_factory=list)
    windows: list[tuple[int, str]] = field(default_factory=list)
    closed: bool = False
    refuse: str | None = None
    extended: list[str] = field(default_factory=list)
    links: int = 1
    unsettled_now: bool = False
    step_ids: list[str] = field(default_factory=list)
    identities: list[Any] = field(default_factory=list)

    def bind_plan(self, digest: str, *, allow_change: bool = False, step_ids: Any = ()) -> None:
        if self.refuse == "plan_changed" and not allow_change:
            raise LedgerError("plan_changed: this task id already ran a different plan")
        self.plan_digests.append(digest)
        self.step_ids.extend(step_ids)

    def extend_plan(self, digest: str, step_ids: Any) -> None:
        if self.refuse == "step_id_reused":
            raise LedgerError("step_id_reused: a step id of an earlier plan of this task")
        self.extended.append(digest)
        self.step_ids.extend(step_ids)

    def plan_count(self) -> int:
        return self.links + len(self.extended)

    def unsettled(self) -> bool:
        return self.unsettled_now

    def bind_window(
        self, *, pid: int, title: str, allow_change: bool = False, identity: Any = None
    ) -> None:
        self.identities.append(identity)
        if self.refuse == "window_changed" and not allow_change:
            raise LedgerError("window_changed: bound to another window")
        self.windows.append((pid, title))

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeSession:
    result: TaskResult | BaseException
    ran: list[Plan] = field(default_factory=list)
    hwnd: int = 0x10
    continuations: list[bool] = field(default_factory=list)

    def live_title(self, ref: str) -> str | None:  # noqa: ARG002 - the fake has no windows
        return None

    def run_task(self, plan: Plan, *, continuation: bool = False) -> TaskResult:
        self.ran.append(plan)
        self.continuations.append(continuation)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@dataclass
class World:
    windows: Sequence[WindowInfo] = (CHROME, NOTEPAD, CONSOLE)
    excluded: tuple[set[int], set[int]] = field(default_factory=lambda: ({CONSOLE.hwnd}, {700}))
    foreground_calls: list[int] = field(default_factory=list)
    foreground_error: Exception | None = None
    ledger_error: Exception | None = None
    ledger: FakeLedger | None = None
    session: FakeSession | None = None
    session_error: Exception | None = None
    bound: list[Bound] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    ledger_links: int = 1

    def deps(self) -> Deps:
        def list_windows() -> Sequence[WindowInfo]:
            self.calls.append("list_windows")
            return self.windows

        def acquire(hwnd: int) -> str:
            self.calls.append("foreground")
            if self.foreground_error is not None:
                raise self.foreground_error
            self.foreground_calls.append(hwnd)
            return "direct"

        def open_ledger(_root: Path, task_id: str) -> Any:
            self.calls.append("ledger")
            if self.ledger_error is not None:
                raise self.ledger_error
            self.ledger = FakeLedger(task_id, links=self.ledger_links)
            return self.ledger

        def build(bound: Bound) -> Any:
            self.calls.append("session")
            self.bound.append(bound)
            if self.session_error is not None:
                raise self.session_error
            assert self.session is not None
            return self.session

        return Deps(
            list_windows=list_windows,
            exclusions=lambda: self.excluded,
            acquire_foreground=acquire,
            open_ledger=open_ledger,
            build_session=build,
        )


def _done(plan: Plan) -> TaskResult:
    results = [
        StepResult(
            step_id=s.id,
            ok=True,
            gate="approve",
            action=None,
            verify_code=None,
            label_kind="t1",
            latency_ms={"observe": 1.0},
            evidence={},
        )
        for s in plan.steps
    ]
    return TaskResult(status="done", results=results, ticked={"g1"})


def _options(tmp_path: Path, plan: Plan, **overrides: Any) -> Options:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan_to_json(plan), encoding="utf-8")
    argv = ["--plan", str(plan_path), "--window", "chrome", "--ledger-dir", str(tmp_path / "l")]
    options = parse_args(argv)
    return options if not overrides else options.replace(**overrides)


def _run(options: Options, world: World, *, stdin: str = "") -> tuple[int, dict[str, Any], str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(options, world.deps(), stdin=io.StringIO(stdin), stdout=stdout, stderr=stderr)
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1, "stdout holds exactly one JSON line"
    return code, json.loads(lines[0]), stderr.getvalue()


def test_a_done_task_exits_0_with_one_json_line_and_sorted_ticks(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)))
    code, out, err = _run(_options(tmp_path, plan), world)
    assert code == EXIT_DONE
    assert out["status"] == "done"
    assert out["ticked"] == ["g1"]
    assert [r["step_id"] for r in out["results"]] == ["s2"]
    assert out["task_id"]
    assert world.calls == ["ledger", "list_windows", "foreground", "session"]
    assert world.foreground_calls == [CHROME.hwnd]
    assert world.ledger is not None
    assert world.ledger.closed is True
    assert world.ledger.windows == [(CHROME.pid, CHROME.title)]
    assert len(world.ledger.plan_digests) == 1
    assert world.bound[0].hwnd == CHROME.hwnd
    assert world.bound[0].pid == CHROME.pid
    assert "hello there" not in err


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("paused", EXIT_PAUSED), ("blocked", EXIT_BLOCKED), ("stopped", EXIT_STOPPED)],
)
def test_exit_codes_follow_the_task_status_and_the_resume_command_is_shown(
    tmp_path: Path, status: Any, exit_code: int
) -> None:
    plan = _plan(_type())
    result = TaskResult(
        status=status,
        results=[],
        ticked=set(),
        code=ErrorCode.GATE_PENDING_USER,
        evidence={"detail": "unanswered"},
    )
    world = World(session=FakeSession(result))
    options = _options(tmp_path, plan)
    code, out, err = _run(options, world)
    assert code == exit_code
    assert out["status"] == status
    assert out["code"] == "gate_pending_user"
    assert f"--task-id {options.task_id}" in err, "the resume command names the task id"
    assert "--ledger-dir" in err


def test_a_plan_over_the_risk_limit_is_refused_before_anything_else(tmp_path: Path) -> None:
    plan = _plan(_type(), _send())
    world = World(session=FakeSession(_done(plan)))
    code, out, _ = _run(_options(tmp_path, plan, max_risk="R1"), world)
    assert code == EXIT_BLOCKED
    assert out["status"] == "blocked"
    assert out["code"] == "gate_blocked"
    assert out["detail"] == "risk_over_limit"
    assert out["step_id"] == "s3"
    assert out["risk"] == "R2"
    assert out["results"] == []
    assert world.calls == [], "no window listing, no foreground, no ledger"


def test_an_unreadable_or_invalid_plan_is_a_usage_error(tmp_path: Path) -> None:
    plan = _plan(_type())
    options = _options(tmp_path, plan)
    options.plan.write_text('{"goal": 1}', encoding="utf-8")
    world = World(session=FakeSession(_done(plan)))
    code, out, err = _run(options, world)
    assert code == EXIT_USAGE
    assert out["status"] == "error"
    assert out["detail"] == "invalid_plan"
    assert "$.goal" in err
    options.plan.unlink()
    code, out, _ = _run(options, world)
    assert code == EXIT_USAGE
    assert out["detail"] == "plan_unreadable"


def test_window_resolution_failures_exit_1_with_the_candidates_on_stderr(
    tmp_path: Path,
) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)), excluded=(set(), set()))
    code, out, err = _run(_options(tmp_path, plan), world)
    assert code == EXIT_USAGE
    assert out["status"] == "error"
    assert out["code"] == "element_not_found"
    assert out["detail"] == "ambiguous_target"
    assert "0x30" in err or "48" in err
    assert world.foreground_calls == []


def test_a_locked_or_rebound_ledger_exits_1_before_any_window_work(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(
        session=FakeSession(_done(plan)),
        ledger_error=LedgerError("ledger_locked: another runner (pid 9) holds this task"),
    )
    code, out, _ = _run(_options(tmp_path, plan), world)
    assert code == EXIT_USAGE
    assert out["detail"] == "ledger_locked"
    assert "list_windows" not in world.calls
    world = World(session=FakeSession(_done(plan)))
    options = _options(tmp_path, plan)
    world.deps()
    world.ledger_error = None

    def refusing(_root: Path, task_id: str) -> Any:
        world.ledger = FakeLedger(task_id, refuse="plan_changed")
        return world.ledger

    deps = world.deps().replace(open_ledger=refusing)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(options, deps, stdin=io.StringIO(), stdout=stdout, stderr=stderr)
    assert code == EXIT_USAGE
    assert json.loads(stdout.getvalue())["detail"] == "plan_changed"
    assert "--allow-plan-change" in stderr.getvalue()
    assert world.ledger is not None
    assert world.ledger.closed is True
    code = run(
        options.replace(allow_plan_change=True),
        deps,
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == EXIT_DONE


def test_foreground_and_session_setup_failures_exit_1_with_their_code(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)), foreground_error=OSError("hung foreground"))
    code, out, _ = _run(_options(tmp_path, plan), world)
    assert code == EXIT_USAGE
    assert out["code"] == "focus_lost"
    world = World(
        session=FakeSession(_done(plan)),
        session_error=SteerError(ErrorCode.PERCEPTION_DEGRADED, "not per-monitor v2"),
    )
    code, out, _ = _run(_options(tmp_path, plan), world)
    assert code == EXIT_USAGE
    assert out["code"] == "perception_degraded"
    assert world.ledger is not None
    assert world.ledger.closed is True


def test_an_interrupt_during_the_task_pauses_and_keeps_the_ledger_closed(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(KeyboardInterrupt()))
    code, out, err = _run(_options(tmp_path, plan), world)
    assert code == EXIT_PAUSED
    assert out["status"] == "paused"
    assert out["code"] == "gate_pending_user"
    assert out["detail"] == "interrupted"
    assert world.ledger is not None
    assert world.ledger.closed is True
    assert "--task-id" in err


def test_an_unexpected_error_exits_70_with_the_traceback_on_stderr_only(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(RuntimeError("COM went away: element 'Amir Khan'")))
    code, out, err = _run(_options(tmp_path, plan), world)
    assert code == EXIT_ERROR
    assert out["status"] == "error"
    assert out["detail"] == "internal_error"
    assert "Amir Khan" not in json.dumps(out)
    assert "RuntimeError" in err
    assert world.ledger is not None
    assert world.ledger.closed is True


def test_the_console_confirmer_is_the_sessions_confirmer(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)))
    _run(_options(tmp_path, plan), world, stdin="no\n")
    bound = world.bound[0]
    assert type(bound.confirmer).__name__ == "ConsoleConfirmer"
    assert bound.options.max_steps == 20
    assert bound.trace is not None


def _step_ids(plan: Plan) -> list[str]:
    return [s.id for s in plan.steps]


def test_the_plan_handed_to_the_session_is_the_file_plan(tmp_path: Path) -> None:
    plan = _plan(_type(), _send())
    world = World(session=FakeSession(_done(plan)))
    _run(_options(tmp_path, plan), world, stdin="no\n")
    assert world.session is not None
    assert _step_ids(world.session.ran[0]) == _step_ids(plan)
    assert isinstance(world.session.ran[0].steps[0], Step)


def test_warm_up_absorbs_a_provider_that_fails_for_a_while_then_answers() -> None:
    """First contact includes a provider that answers E_FAIL for a moment after a foreground
    change (overlay runs 2026-11-04): a `perception_timeout` observe is "not yet", kept polling
    until the cap; the cap with no answer raises the last error."""
    clock = {"t": 0.0}
    answers = iter([None, None, None, 60, 60, 60])

    def observe() -> Snapshot:
        count = next(answers)
        if count is None:
            raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "uia fetch failed: E_FAIL")
        rows: list[Row] = [(i, "Group", f"n{i}", (42, i), None) for i in range(count)]
        return build(rows, snapshot_id="s", taken_at=clock["t"])

    def sleep(s: float) -> None:
        clock["t"] += s

    settled = warm_up(observe, cap_s=10.0, monotonic=lambda: clock["t"], sleep=sleep)
    assert settled.node_count == 60
    assert clock["t"] >= 3 * 0.25, "the failed looks waited a poll each"

    def never() -> Snapshot:
        raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "uia fetch failed: E_FAIL")

    clock["t"] = 0.0
    with pytest.raises(SteerError) as caught:
        warm_up(never, cap_s=1.0, monotonic=lambda: clock["t"], sleep=sleep)
    assert caught.value.code is ErrorCode.PERCEPTION_TIMEOUT
    assert clock["t"] >= 1.0
