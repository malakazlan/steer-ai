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
from steerai.orchestrator.contracts import Plan, Step, StepResult, Subgoal, Target, subgoal_verbs
from steerai.orchestrator.loop import TaskResult
from steerai.orchestrator.plan_io import plan_from_bytes, plan_to_json
from steerai.planner.contracts import DoneStep, OpenSubgoal, PlannerError, PlanOutcome, PlanRequest
from steerai.probes.windows import WindowInfo
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
    titles: dict[str, str] = field(default_factory=dict)

    def live_title(self, ref: str) -> str | None:
        return self.titles.get(ref)

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
    # each link keeps its plan's subgoal verbs, the same map the session uses (#51)
    assert rig.ledger.verb_maps == [subgoal_verbs(first.plan), subgoal_verbs(cont.plan)]
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


def test_an_existing_continuation_file_stops_before_the_model_call(tmp_path: Path) -> None:
    rig, first, _cont = _two_plan_rig(tmp_path)
    (tmp_path / "l").mkdir(parents=True, exist_ok=True)
    (tmp_path / "l" / "rp-1.plan.2.json").write_text("{}", encoding="utf-8")
    code, out, _err = rig.run(f"{_go(first)}\n")
    assert out["detail"] == "plan_exists"
    assert len(rig.planner.requests) == 1, "no second model call"
    assert code == EXIT_STOPPED


# ---- the continuation's own gates: chain, risk limit, preview, ask, idle, foreground ---------


def test_the_preview_names_the_reached_subgoals_and_marks_a_rewritten_condition(
    tmp_path: Path,
) -> None:
    """Decision Q1: plan 1's g2 waited on "x"; the continuation rewrote it for this screen."""
    rig, first, cont = _two_plan_rig(tmp_path)
    _code, _out, err = rig.run(f"{_go(first)}\n{_go(cont)}\n")
    assert "continuation 1/2; subgoals reached: g1" in err
    assert "subgoal g2: playing when" in err
    assert err.count("(condition rewritten)") == 1


def test_a_chain_refusal_blocks_with_its_token_and_runs_nothing_more(tmp_path: Path) -> None:
    rig, first, _cont = _two_plan_rig(tmp_path)
    base = rig.world.deps

    def deps() -> Any:
        built = base()
        open_ledger = built.open_ledger

        def refusing(root: Path, task_id: str) -> Any:
            ledger: Any = open_ledger(root, task_id)  # Any: the test's FakeLedger
            ledger.refuse = "step_id_reused"
            return ledger

        return built.replace(open_ledger=refusing)

    rig.world.deps = deps  # type: ignore[method-assign]
    code, out, _err = rig.run(f"{_go(first)}\n")
    assert code == EXIT_BLOCKED
    assert (out["status"], out["code"], out["detail"]) == (
        "blocked",
        "gate_blocked",
        "step_id_reused",
    )
    assert len(rig.session.ran) == 1
    assert [r["step_id"] for r in out["results"]] == ["s1"]
    # the link is proposed before its file is written: a refused link leaves no file (#57)
    assert not (tmp_path / "l" / "rp-1.plan.2.json").exists()


def test_a_continuation_over_the_risk_limit_is_refused_before_its_ask(tmp_path: Path) -> None:
    first = _outcome(_first())
    risky = Plan(
        goal=GOAL,
        subgoals=_continuation().subgoals,
        steps=(
            Step(
                id="p2.s1",
                subgoal="g2",
                verb="click",
                target=Target(role="Link", name="Drake - Video"),
                risk="R2",
                expect=TitleMatches(pattern="Drake"),
            ),
        ),
    )
    cont = _outcome(risky)
    rig = Rig(ScriptedSession([_unfinished(_step("s1"))]), ScriptedPlanner([first, cont]), tmp_path)
    code, out, err = rig.run(f"{_go(first)}\n{_go(cont)}\n", "--max-risk", "R1")
    assert code == EXIT_BLOCKED
    assert (out["status"], out["detail"]) == ("blocked", "risk_over_limit")
    assert "p2.s1" in err
    assert _go(cont)[3:] not in err, "never previewed or asked"
    assert len(rig.session.ran) == 1


@dataclass
class ScriptedConfirmer:
    answers: list[str]
    asked: list[str] = field(default_factory=list)

    def ask_go(self, go_id: str) -> str:
        self.asked.append(go_id)
        return self.answers[len(self.asked) - 1]


def test_a_kill_at_the_continuations_ask_blocks_killed(tmp_path: Path) -> None:
    rig, first, cont = _two_plan_rig(tmp_path)
    confirmer = ScriptedConfirmer(["go", "killed"])
    base = rig.world.deps

    def deps() -> Any:
        return base().replace(make_confirmer=lambda _i, _e, _m: confirmer)

    rig.world.deps = deps  # type: ignore[method-assign]
    code, out, err = rig.run("")
    assert code == EXIT_BLOCKED
    assert (out["status"], out["detail"]) == ("blocked", "killed")
    assert confirmer.asked == [_go(first)[3:], _go(cont)[3:]]
    assert len(rig.session.ran) == 1
    assert "resume:" in err


@dataclass
class HeldMonitor:
    """Idle and alive until the session says otherwise after plan 1."""

    clock: dict[str, float]
    killed: bool = False
    busy: bool = False
    lost: bool = False

    def kill_requested(self) -> bool:
        return self.killed

    def last_user_input(self) -> float:
        return self.clock["t"] if self.busy else -100.0

    def healthy(self) -> bool:
        return not self.lost

    def detail(self) -> str:
        return "monitor_lost"

    def maintain(self) -> None:
        return None

    def counts(self) -> dict[str, int]:
        return {}

    def mark_ask_open(self) -> None:
        return None

    def mark_ask_closed(self) -> None:
        return None

    def close(self) -> None:
        return None


def _held(tmp_path: Path, hold: str, *, answered: bool = False) -> tuple[int, dict[str, Any], Rig]:
    """`answered`: both asks say go (a scripted confirmer), so only the idle wait between the
    continuation's go and its first step can see the person."""
    rig, first, cont = _two_plan_rig(tmp_path)
    clock = {"t": 1000.0}
    monitor = HeldMonitor(clock)
    session_run = rig.session.run_task

    def run_task(plan: Plan, *, continuation: bool = False) -> TaskResult:
        result = session_run(plan, continuation=continuation)
        setattr(monitor, hold, True)  # the person acts while plan 1 ends
        return result

    rig.session.run_task = run_task  # type: ignore[method-assign]
    base = rig.world.deps

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    def deps() -> Any:
        built = base().replace(
            start_monitor=lambda _o: monitor, monotonic=lambda: clock["t"], sleep=sleep
        )
        if answered:
            confirmer = ScriptedConfirmer(["go", "go"])
            built = built.replace(make_confirmer=lambda _i, _e, _m: confirmer)
        return built

    rig.world.deps = deps  # type: ignore[method-assign]
    code, out, _err = rig.run(f"{_go(first)}\n{_go(cont)}\n")
    return code, out, rig


def test_a_kill_during_the_continuations_ask_blocks_and_runs_nothing_more(
    tmp_path: Path,
) -> None:
    """The console ask watches the kill key: a go typed after it never counts."""
    code, out, rig = _held(tmp_path, "killed")
    assert code == EXIT_BLOCKED
    assert (out["status"], out["detail"]) == ("blocked", "killed")
    assert len(rig.session.ran) == 1


def test_a_kill_after_the_continuations_go_blocks_before_its_first_step(tmp_path: Path) -> None:
    code, out, rig = _held(tmp_path, "killed", answered=True)
    assert code == EXIT_BLOCKED
    assert (out["status"], out["detail"]) == ("blocked", "killed")
    assert len(rig.session.ran) == 1
    assert OPENED not in rig.world.foreground_calls


def test_a_person_busy_before_the_continuation_pauses_takeover(tmp_path: Path) -> None:
    code, out, rig = _held(tmp_path, "busy")
    assert code == EXIT_PAUSED
    assert (out["status"], out["detail"]) == ("paused", "takeover")
    assert len(rig.session.ran) == 1
    assert OPENED not in rig.world.foreground_calls, "never tapped while the person works"


def test_a_monitor_lost_before_the_continuation_blocks_with_its_detail(tmp_path: Path) -> None:
    code, out, rig = _held(tmp_path, "lost", answered=True)
    assert code == EXIT_BLOCKED
    assert (out["status"], out["code"], out["detail"]) == (
        "blocked",
        "gate_blocked",
        "monitor_lost",
    )
    assert len(rig.session.ran) == 1


def _desktop_rig(
    tmp_path: Path, *, opens: bool, titles: dict[str, str] | None = None
) -> tuple[int, dict[str, Any], Rig]:
    desktop = WindowInfo(hwnd=0x50, title="Program Manager", pid=900)
    first_plan = _first() if opens else _continuation(prefix="p1", subgoal="g2")
    if not opens:
        first_plan = Plan(
            goal=GOAL,
            subgoals=(
                Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT)),
                *first_plan.subgoals,
            ),
            steps=first_plan.steps,
        )
    first, cont = _outcome(first_plan), _outcome(_continuation())
    one = "s1" if opens else "p1.s1"
    session = ScriptedSession(
        [_unfinished(_step(one)), _done("p2.s1")], hwnd=desktop.hwnd, titles=dict(titles or {})
    )
    rig = Rig(session, ScriptedPlanner([first, cont]), tmp_path)
    base = rig.world.deps

    def deps() -> Any:
        return base().replace(desktop=lambda: desktop)

    rig.world.deps = deps  # type: ignore[method-assign]
    argv = ["--goal", GOAL, "--desktop", "--ledger-dir", str(tmp_path / "l"), "--task-id", "rp-1"]
    code, out, err = rig.run(f"{_go(first)}\n{_go(cont)}\n", options=parse_args(argv))
    assert code == EXIT_DONE, err
    return code, out, rig


def test_a_desktop_task_that_opened_a_window_brings_it_to_the_front(tmp_path: Path) -> None:
    """Hunter RP-14: once the task left the desktop, plan 2 starts with its window in front."""
    _code, _out, rig = _desktop_rig(tmp_path, opens=True)
    assert rig.world.foreground_calls == [OPENED]


def test_a_desktop_task_still_on_the_desktop_never_taps_it(tmp_path: Path) -> None:
    _code, _out, rig = _desktop_rig(tmp_path, opens=False)
    assert rig.world.foreground_calls == []


def test_a_session_locked_between_plans_stops_with_the_merged_results(tmp_path: Path) -> None:
    """Issue #31: locked while the planner ran; the continuation is never tapped or run."""
    rig, first, cont = _two_plan_rig(tmp_path)
    reads: list[int] = []

    def locked() -> bool:
        reads.append(1)
        return len(reads) > 1  # unlocked at the start, locked before plan 2

    base_deps = rig.world.deps
    rig.world.deps = lambda: base_deps().replace(session_locked=locked)  # type: ignore[method-assign]
    code, out, _err = rig.run(f"{_go(first)}\n{_go(cont)}\n")
    assert code == EXIT_STOPPED
    assert (out["code"], out["detail"]) == ("secure_desktop", "session_locked")
    assert [r["step_id"] for r in out["results"]] == ["s1"]
    assert len(rig.session.ran) == 1
    assert OPENED not in rig.world.foreground_calls


def test_a_locked_session_is_never_read_or_planned_for_a_replan(tmp_path: Path) -> None:
    """PR #33 review item 2: the window's content never leaves the machine for a continuation
    while the session is locked."""
    rig, first, _cont = _two_plan_rig(tmp_path)
    reads: list[int] = []

    def locked() -> bool:
        reads.append(1)
        return len(reads) > 1

    base_deps = rig.world.deps
    rig.world.deps = lambda: base_deps().replace(session_locked=locked)  # type: ignore[method-assign]
    _code, out, _err = rig.run(f"{_go(first)}\n")
    assert out["detail"] == "session_locked"
    assert rig.screens == [0x10], "the continuation's screen is never read"
    assert len(rig.planner.requests) == 1


def test_the_continuation_sees_the_tasks_windows_by_reference(tmp_path: Path) -> None:
    """Step-window packet item 6: the verified opens and the start window, live titles."""
    rig, first, cont = _two_plan_rig(tmp_path)
    rig.session.titles = {"start": "Inbox - Chrome", "opened:s1": "drake - YouTube"}
    rig.run(f"{_go(first)}\n{_go(cont)}\n")
    assert list(rig.planner.requests[0].windows) == [("start", "YouTube - Google Chrome")]
    assert list(rig.planner.requests[1].windows) == [
        ("start", "Inbox - Chrome"),
        ("opened:s1", "drake - YouTube"),
    ]


def test_a_desktop_task_never_lists_a_start_window_to_the_planner(tmp_path: Path) -> None:
    """Step-window SW-05: the shell's desktop is never a window a step may go back to."""
    titles = {"start": "Program Manager", "opened:s1": "drake - YouTube"}
    _code, _out, rig = _desktop_rig(tmp_path, opens=True, titles=titles)
    assert list(rig.planner.requests[0].windows) == []
    assert list(rig.planner.requests[1].windows) == [("opened:s1", "drake - YouTube")]


def test_a_window_whose_identity_no_longer_holds_is_not_listed(tmp_path: Path) -> None:
    """SW-36: `live_title` None (the window is not the task's any more) drops the line."""
    rig, first, cont = _two_plan_rig(tmp_path)
    rig.session.titles = {"opened:s1": "drake - YouTube"}
    rig.run(f"{_go(first)}\n{_go(cont)}\n")
    assert list(rig.planner.requests[1].windows) == [("opened:s1", "drake - YouTube")]
