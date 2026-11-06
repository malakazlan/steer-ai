"""The runner's replanning (packet replan-2026-11-06; schema gate items 1, 3, 6, 7 and Q1; hunter
RP-14/15/17/19/22/33/39/42/50): a `--goal` run whose plan ended unfinished, or on a target
missing before the gate, is planned again from the window the task reached, previewed, run only
after its own `go:<id>`, with the results merged over the plans. The planner and the session
are fakes."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import Plan, Step, StepResult, Subgoal, Target
from steerai.orchestrator.loop import TaskResult
from steerai.orchestrator.plan_io import plan_from_bytes, plan_to_json
from steerai.planner.contracts import DoneStep, OpenSubgoal, PlannerError, PlanOutcome, PlanRequest
from steerai.run import EXIT_BLOCKED, EXIT_DONE, EXIT_PAUSED, EXIT_STOPPED, parse_args, run
from steerai.verify.predicates import TitleMatches, WindowExists
from tests.orchestrator.test_run import FakeLedger, World

GOAL = "search YouTube for drake latest video and play the first video"
YT = "drake latest video - YouTube - Google Chrome"
OPENED = 0x30
SENTINEL = "sentinel-words-7f3a"


def _first() -> Plan:
    return Plan(
        goal=GOAL,
        subgoals=(
            Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT)),
            Subgoal(id="g2", desc="the first video plays", condition=TitleMatches(pattern="x")),
        ),
        steps=(
            Step(
                id="s1",
                subgoal="g1",
                verb="open",
                target=Target(role="App", name="chrome"),
                risk="R1",
                params={"site": "youtube_search", "text": SENTINEL},
            ),
        ),
    )


def _continuation(prefix: str = "p2", subgoal: str = "g2", **kw: Any) -> Plan:
    return Plan(
        goal=kw.pop("goal", GOAL),
        subgoals=(Subgoal(id=subgoal, desc="playing", condition=TitleMatches(pattern="Drake")),),
        steps=(
            Step(
                id=kw.pop("step_id", f"{prefix}.s1"),
                subgoal=subgoal,
                verb="click",
                target=Target(role="Link", name="Drake - Video"),
                risk="R0",
                expect=TitleMatches(pattern="Drake"),
            ),
        ),
    )


def _outcome(plan: Plan) -> PlanOutcome:
    data = plan_to_json(plan).encode("utf-8")
    return PlanOutcome(
        plan=plan_from_bytes(data),
        data=data,
        model="claude-opus-5-20260301",
        prompt_version="planner-7",
        latency_ms=900.0,
    )


def _go(outcome: PlanOutcome) -> str:
    return "go:" + hashlib.sha256(outcome.data).hexdigest()[:12]


def _step(
    step_id: str, *, ok: bool = True, risk: str = "R0", dispatched: bool = True
) -> StepResult:
    action = (
        ActionResult(
            verb="click", delivery=Delivery.DELIVERED_UNVERIFIED, code=ErrorCode.VERIFY_TIMEOUT
        )
        if dispatched
        else None
    )
    return StepResult(
        step_id=step_id,
        ok=ok,
        gate="approve",
        action=action,
        verify_code=None if ok else ErrorCode.VERIFY_TIMEOUT,
        label_kind="t1" if ok else None,
        latency_ms={},
        evidence={"risk": risk, "text": SENTINEL},
        code=None if ok else ErrorCode.VERIFY_TIMEOUT,
    )


def _unfinished(*results: StepResult, ticked: set[str] | None = None) -> TaskResult:
    return TaskResult(
        status="stopped",
        results=list(results),
        ticked={"g1"} if ticked is None else ticked,
        code=ErrorCode.VERIFY_FAILED,
        evidence={"detail": "unfinished"},
    )


def _done(step_id: str, subgoal: str = "g2") -> TaskResult:
    return TaskResult(status="done", results=[_step(step_id)], ticked={subgoal})


@dataclass
class ScriptedSession:
    """One result per plan; the first plan's open moves the task into the opened window."""

    results: list[TaskResult]
    hwnd: int = 0x10
    ran: list[tuple[Plan, bool]] = field(default_factory=list)

    def run_task(self, plan: Plan, *, continuation: bool = False) -> TaskResult:
        self.ran.append((plan, continuation))
        result = self.results[len(self.ran) - 1]
        if any(s.verb == "open" for s in plan.steps):
            self.hwnd = OPENED
        return result


@dataclass
class ScriptedPlanner:
    outcomes: list[PlanOutcome | Exception]
    requests: list[PlanRequest] = field(default_factory=list)

    def __call__(self, request: PlanRequest) -> PlanOutcome:
        self.requests.append(request)
        result = self.outcomes[len(self.requests) - 1]
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class Rig:
    session: ScriptedSession
    planner: ScriptedPlanner
    tmp: Path
    world: World = field(default_factory=World)
    screens: list[int] = field(default_factory=list)
    screen_error: Exception | None = None
    unsettled: bool = False

    def options(self, *extra: str) -> Any:
        argv = ["--goal", GOAL, "--window", "chrome", "--ledger-dir", str(self.tmp / "l")]
        return parse_args(
            [*argv, "--task-id", "rp-1", "--trace", str(self.tmp / "t.jsonl"), *extra]
        )

    def run(self, stdin: str, *extra: str, options: Any = None) -> tuple[int, dict[str, Any], str]:
        self.world.session = self.session  # type: ignore[assignment]  # the scripted protocol

        def screen(hwnd: int) -> tuple[str, list[str]]:
            self.screens.append(hwnd)
            if self.screen_error is not None and len(self.screens) > 1:
                raise self.screen_error
            return "YouTube - Google Chrome", ['1  Link "Drake - Video"   [0,0,10,10]']

        base = self.world.deps()

        def open_ledger(root: Path, task_id: str) -> Any:
            ledger: Any = base.open_ledger(root, task_id)  # Any: the test's FakeLedger
            ledger.unsettled_now = self.unsettled
            return ledger

        deps = base.replace(screen=screen, planner=self.planner, open_ledger=open_ledger)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = run(
            options or self.options(*extra),
            deps,
            stdin=io.StringIO(stdin),
            stdout=stdout,
            stderr=stderr,
        )
        lines = stdout.getvalue().splitlines()
        assert len(lines) == 1, "stdout holds exactly one JSON line"
        return code, json.loads(lines[0]), stderr.getvalue()

    @property
    def ledger(self) -> FakeLedger:
        assert self.world.ledger is not None
        return self.world.ledger


def _two_plan_rig(
    tmp_path: Path, second: TaskResult | None = None
) -> tuple[Rig, PlanOutcome, PlanOutcome]:
    first, cont = _outcome(_first()), _outcome(_continuation())
    session = ScriptedSession([_unfinished(_step("s1")), second or _done("p2.s1")])
    return Rig(session, ScriptedPlanner([first, cont]), tmp_path), first, cont


# ---- the continuation ---------------------------------------------------------------------------


def test_an_unfinished_run_is_replanned_previewed_and_run_after_its_own_go(tmp_path: Path) -> None:
    rig, first, cont = _two_plan_rig(tmp_path)
    code, out, err = rig.run(f"{_go(first)}\n{_go(cont)}\n")
    assert code == EXIT_DONE, err
    assert out["status"] == "done"
    assert out["ticked"] == ["g1", "g2"]
    assert [r["step_id"] for r in out["results"]] == ["s1", "p2.s1"]
    request = rig.planner.requests[1]
    assert request.goal == GOAL
    assert request.continuation == 1
    assert list(request.done) == [DoneStep(verb="open", role="App", name="chrome")]
    assert list(request.remaining) == [OpenSubgoal(id="g2", desc="the first video plays")]
    assert rig.screens == [0x10, OPENED], "the second screen is the window the task reached"
    second_file = tmp_path / "l" / "rp-1.plan.2.json"
    assert second_file.read_bytes() == cont.data, "the exact bytes of the continuation"
    assert rig.ledger.plan_digests == [hashlib.sha256(first.data).hexdigest()]
    assert rig.ledger.extended == [hashlib.sha256(cont.data).hexdigest()]
    assert [c for _p, c in rig.session.ran] == [False, True]
    assert rig.ledger.step_ids == ["s1", "p2.s1"], "every plan's ids reach the ledger"
    assert rig.world.foreground_calls[-1] == OPENED, "plan 2 starts with its window in front"
    assert "continuation 1/2" in err
    assert "plan " + _go(cont)[3:] in err


def test_the_replan_is_traced_without_the_goal_or_the_screen(tmp_path: Path) -> None:
    rig, first, cont = _two_plan_rig(tmp_path)
    rig.run(f"{_go(first)}\n{_go(cont)}\n")
    lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
    replans = [json.loads(line) for line in lines if '"kind": "replan"' in line]
    assert replans == [{"kind": "replan", "task_id": "rp-1", "number": 1, "trigger": "unfinished"}]
    text = "\n".join(lines)
    assert GOAL not in text
    assert "Drake - Video" not in text
    assert SENTINEL not in text


def test_the_progress_sent_to_the_planner_carries_no_text_or_params(tmp_path: Path) -> None:
    """Item 5 sentinel, end to end: the user turn built from the runner's request."""
    from steerai.planner.claude_planner import build_prompt  # noqa: PLC0415

    rig, first, cont = _two_plan_rig(tmp_path)
    rig.run(f"{_go(first)}\n{_go(cont)}\n")
    _system, user = build_prompt(rig.planner.requests[1], tag="t")
    assert SENTINEL not in user


def test_a_missing_target_before_the_gate_replans_with_the_failure(tmp_path: Path) -> None:
    missing = StepResult(
        step_id="s2",
        ok=False,
        gate="unreached",
        action=None,
        verify_code=None,
        label_kind=None,
        latency_ms={},
        evidence={"detail": "no_match"},
        code=ErrorCode.ELEMENT_NOT_FOUND,
    )
    plan = Plan(
        goal=GOAL,
        subgoals=_first().subgoals,
        steps=(
            *_first().steps,
            Step(
                id="s2",
                subgoal="g2",
                verb="click",
                target=Target(role="Link", name="Play"),
                risk="R0",
                expect=TitleMatches(pattern="x"),
            ),
        ),
    )
    first, cont = _outcome(plan), _outcome(_continuation())
    stopped = TaskResult(
        status="stopped",
        results=[_step("s1"), missing],
        ticked={"g1"},
        code=ErrorCode.ELEMENT_NOT_FOUND,
        evidence={"detail": "no_match"},
    )
    rig = Rig(ScriptedSession([stopped, _done("p2.s1")]), ScriptedPlanner([first, cont]), tmp_path)
    code, _out, err = rig.run(f"{_go(first)}\n{_go(cont)}\n")
    assert code == EXIT_DONE, err
    failure = rig.planner.requests[1].failure
    assert failure is not None
    assert (failure.step_id, failure.code) == ("s2", "element_not_found")


# ---- what never replans ------------------------------------------------------------------------


def test_an_unverified_r2_effect_stops_without_a_planner_call(tmp_path: Path) -> None:
    first = _outcome(_first())
    run1 = _unfinished(_step("s1", ok=False, risk="R2"), ticked=set())
    rig = Rig(ScriptedSession([run1]), ScriptedPlanner([first]), tmp_path)
    code, out, err = rig.run(f"{_go(first)}\n")
    assert code == EXIT_STOPPED
    assert (out["status"], out["code"], out["detail"]) == (
        "stopped",
        "verify_failed",
        "unverified_effect",
    )
    assert len(rig.planner.requests) == 1
    assert "check" in err


def test_an_unsettled_ledger_attempt_stops_without_a_planner_call(tmp_path: Path) -> None:
    rig, first, _cont = _two_plan_rig(tmp_path)
    rig.unsettled = True
    code, out, _err = rig.run(f"{_go(first)}\n")
    assert out["detail"] == "unverified_effect"
    assert len(rig.planner.requests) == 1
    assert code == EXIT_STOPPED


def test_the_third_trigger_stops_replan_limit_without_a_planner_call(tmp_path: Path) -> None:
    first, second, third = (
        _outcome(_first()),
        _outcome(_continuation("p2")),
        _outcome(_continuation("p3")),
    )
    session = ScriptedSession(
        [
            _unfinished(_step("s1")),
            _unfinished(_step("p2.s1", ok=False), ticked=set()),
            _unfinished(_step("p3.s1", ok=False), ticked=set()),
        ]
    )
    rig = Rig(session, ScriptedPlanner([first, second, third]), tmp_path)
    code, out, err = rig.run(f"{_go(first)}\n{_go(second)}\n{_go(third)}\n")
    assert code == EXIT_STOPPED
    assert (out["code"], out["detail"]) == ("budget_exceeded", "replan_limit")
    assert len(rig.planner.requests) == 3
    assert [r["step_id"] for r in out["results"]] == ["s1", "p2.s1", "p3.s1"]
    assert "rp-1.plan.3.json" in err.splitlines()[-1], "the hint names the last bound plan"


def test_a_plan_file_run_never_replans(tmp_path: Path) -> None:
    plan_path = tmp_path / "p.json"
    plan_path.write_text(plan_to_json(_first()), encoding="utf-8")
    rig = Rig(ScriptedSession([_unfinished(_step("s1"))]), ScriptedPlanner([]), tmp_path)
    options = parse_args(
        ["--plan", str(plan_path), "--window", "chrome", "--ledger-dir", str(tmp_path / "l")]
    )
    code, out, _err = rig.run("", options=options)
    assert out["detail"] == "unfinished"
    assert rig.planner.requests == []
    assert code == EXIT_STOPPED


def test_a_resumed_later_link_runs_as_a_continuation(tmp_path: Path) -> None:
    plan_path = tmp_path / "rp-1.plan.2.json"
    plan_path.write_text(plan_to_json(_continuation()), encoding="utf-8")
    rig = Rig(ScriptedSession([_done("p2.s1")]), ScriptedPlanner([]), tmp_path)
    rig.world.ledger_links = 2
    options = parse_args(
        ["--plan", str(plan_path), "--window", "chrome", "--ledger-dir", str(tmp_path / "l")]
    )
    rig.run("", options=options)
    assert [c for _p, c in rig.session.ran] == [True]


# ---- a replan that ends the task keeps what did run (RP-19, RP-22) ---------------------------


def _ended(
    tmp_path: Path, stdin_second: str, **rig_changes: Any
) -> tuple[int, dict[str, Any], str, Rig]:
    rig, first, cont = _two_plan_rig(tmp_path)
    for name, value in rig_changes.items():
        setattr(rig, name, value)
    second = stdin_second.replace("GO2", _go(cont))
    code, out, err = rig.run(f"{_go(first)}\n{second}")
    assert [r["step_id"] for r in out["results"]] == ["s1"], "plan 1's results stay"
    assert out["ticked"] == ["g1"]
    return code, out, err, rig


def test_a_declined_continuation_blocks_with_the_merged_results(tmp_path: Path) -> None:
    code, out, err, _rig = _ended(tmp_path, "no\n")
    assert code == EXIT_BLOCKED
    assert (out["status"], out["detail"]) == ("blocked", "plan_declined")
    assert "resume:" not in err.splitlines()[-1]


def test_a_stale_go_for_the_first_plan_never_runs_the_second(tmp_path: Path) -> None:
    rig, first, _cont = _two_plan_rig(tmp_path)
    code, out, _err = rig.run(f"{_go(first)}\n{_go(first)}\n")
    assert code == EXIT_PAUSED
    assert out["detail"] == "plan_unanswered"
    assert len(rig.session.ran) == 1


def test_a_planner_failure_on_a_replan_stops_with_its_token(tmp_path: Path) -> None:
    first = _outcome(_first())
    failing = PlannerError(ErrorCode.MODEL_ERROR, "no_steps")
    rig = Rig(
        ScriptedSession([_unfinished(_step("s1"))]), ScriptedPlanner([first, failing]), tmp_path
    )
    code, out, _err = rig.run(f"{_go(first)}\n")
    assert code == EXIT_STOPPED
    assert (out["status"], out["code"], out["detail"]) == ("stopped", "model_error", "no_steps")
    assert [r["step_id"] for r in out["results"]] == ["s1"]
    assert not (tmp_path / "l" / "rp-1.plan.2.json").exists()


def test_an_invalid_continuation_is_refused_before_it_is_written_or_bound(tmp_path: Path) -> None:
    first, bad = _outcome(_first()), _outcome(_continuation(step_id="s1"))
    rig = Rig(ScriptedSession([_unfinished(_step("s1"))]), ScriptedPlanner([first, bad]), tmp_path)
    code, out, err = rig.run(f"{_go(first)}\n")
    assert code == EXIT_STOPPED
    assert out["detail"] == "invalid_plan"
    assert "steps[0].id" in err
    assert not (tmp_path / "l" / "rp-1.plan.2.json").exists()
    assert rig.ledger.extended == []


def test_a_screen_failure_between_plans_stops_with_its_code(tmp_path: Path) -> None:
    gone = SteerError(ErrorCode.APP_CRASHED, "the window is gone")
    code, out, _err, rig = _ended(tmp_path, "", screen_error=gone)
    assert code == EXIT_STOPPED
    assert (out["code"], out["detail"]) == ("app_crashed", "observe_raised")
    assert len(rig.planner.requests) == 1


def test_a_foreground_failure_between_plans_stops_focus_lost(tmp_path: Path) -> None:
    rig, first, cont = _two_plan_rig(tmp_path)
    calls: list[int] = []
    base_deps = rig.world.deps

    def deps() -> Any:
        built = base_deps()

        def acquire(hwnd: int) -> str:
            calls.append(hwnd)
            if hwnd == OPENED:
                raise OSError("refused")
            return "direct"

        return built.replace(acquire_foreground=acquire)

    rig.world.deps = deps  # type: ignore[method-assign]
    code, out, _err = rig.run(f"{_go(first)}\n{_go(cont)}\n")
    assert code == EXIT_STOPPED
    assert (out["code"], out["detail"]) == ("focus_lost", "foreground")
    assert [r["step_id"] for r in out["results"]] == ["s1"]
    assert len(rig.session.ran) == 1
