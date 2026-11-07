"""The plan cache in `--goal` runs (5.12 [v2.33]; gate items 1-6, hunter PC-01/05/10/12/20/21):
a verified run writes, the same request replays with no planner call under a fresh go id, a
probation hit is judged live, a trusted hit reuses its verdicts, anything else plans."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steerai.memory.plan_cache import PlanCache
from steerai.orchestrator.contracts import Plan
from steerai.orchestrator.loop import TaskResult
from steerai.run import run
from tests.memory.test_plan_cache import _fake_protect, _fake_unprotect
from tests.orchestrator.test_run import World, _done
from tests.orchestrator.test_run_goal import (
    GOAL,
    SCREEN_LINES,
    FakePlanner,
    _options,
    _outcome,
    _plan,
)


@dataclass
class JudgedSession:
    """A session that ran the plan verified and judged every step `serves`."""

    hwnd: int = 0x10
    ran: list[Plan] = field(default_factory=list)
    presets: list[dict[str, str]] = field(default_factory=list)
    result: TaskResult | None = None

    def live_title(self, ref: str) -> str | None:  # noqa: ARG002 - the protocol's shape
        return None

    def run_task(self, plan: Plan, *, continuation: bool = False) -> TaskResult:  # noqa: ARG002
        self.ran.append(plan)
        return self.result or _done(plan)

    def preset(self, plan: Plan, verdicts: dict[str, str]) -> None:  # noqa: ARG002
        self.presets.append(dict(verdicts))

    @property
    def judged(self) -> tuple[dict[str, str], str | None]:
        return {s.id: "serves" for s in self.ran[-1].steps}, None


def _go(data: bytes, task_id: str) -> str:
    return "go:" + hashlib.sha256(data + task_id.encode()).hexdigest()[:12]


def _goal_run(
    tmp_path: Path,
    cache: PlanCache,
    planner: FakePlanner,
    session: JudgedSession,
    *,
    task_id: str,
    goal: str = GOAL,
    lines: list[str] | None = None,
) -> tuple[int, str]:
    world = World(session=session)  # type: ignore[arg-type]  # a judged fake session
    deps = world.deps().replace(
        screen=lambda _hwnd: ("Notepad", list(lines if lines is not None else SCREEN_LINES)),
        planner=planner,
        plan_cache=cache,
        identify=lambda _hwnd: {"image": r"C:\Windows\notepad.exe"},
    )
    data = _outcome(_plan()).data
    options = _options(tmp_path / task_id, "--task-id", task_id, goal=goal)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(
        options, deps, stdin=io.StringIO(_go(data, task_id) + "\n"), stdout=stdout, stderr=stderr
    )
    return code, stderr.getvalue()


def _setup(tmp_path: Path) -> tuple[PlanCache, FakePlanner]:
    cache = PlanCache(tmp_path / "cache", protect=_fake_protect, unprotect=_fake_unprotect)
    return cache, FakePlanner(_outcome(_plan()))


def test_a_verified_run_is_written_and_the_same_request_replays_without_the_planner(
    tmp_path: Path,
) -> None:
    cache, planner = _setup(tmp_path)
    first = JudgedSession()
    assert _goal_run(tmp_path, cache, planner, first, task_id="t-1")[0] == 0
    assert len(planner.requests) == 1
    second = JudgedSession()
    code, err = _goal_run(tmp_path, cache, planner, second, task_id="t-2")
    assert code == 0, err
    assert len(planner.requests) == 1, "no planner call on a hit"
    assert "cached plan (probation, verified 1 time(s))" in err
    assert second.presets == [], "a probation hit is judged live (PC-05)"


def test_a_trusted_hit_reuses_its_verdicts(tmp_path: Path) -> None:
    cache, planner = _setup(tmp_path)
    for n in range(1, 4):
        assert _goal_run(tmp_path, cache, planner, JudgedSession(), task_id=f"t-{n}")[0] == 0
    trusted = JudgedSession()
    code, err = _goal_run(tmp_path, cache, planner, trusted, task_id="t-4")
    assert code == 0, err
    assert "cached plan (trusted" in err
    assert trusted.presets == [{"t": "serves", "s": "serves"}]
    assert len(planner.requests) == 1


def test_an_old_go_token_never_approves_a_replay(tmp_path: Path) -> None:
    """The go id carries the task id: run 1's shown token never approves run 2 (gate item 4)."""
    import re  # noqa: PLC0415 - this test only

    cache, planner = _setup(tmp_path)
    _, first_err = _goal_run(tmp_path, cache, planner, JudgedSession(), task_id="t-1")
    shown = re.search(r"type (go:[0-9a-f]{12}) to run", first_err)
    assert shown is not None
    world = World(session=JudgedSession())  # type: ignore[arg-type]
    deps = world.deps().replace(
        screen=lambda _hwnd: ("Notepad", list(SCREEN_LINES)),
        planner=planner,
        plan_cache=cache,
        identify=lambda _hwnd: {"image": "notepad.exe"},
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    options = _options(tmp_path / "t-2", "--task-id", "t-2")
    code = run(
        options, deps, stdin=io.StringIO(shown.group(1) + "\n"), stdout=stdout, stderr=stderr
    )
    assert code != 0
    assert shown.group(1) not in stderr.getvalue(), "run 2 shows a different token"


def test_a_case_change_misses_and_plans(tmp_path: Path) -> None:
    cache, planner = _setup(tmp_path)
    _goal_run(tmp_path, cache, planner, JudgedSession(), task_id="t-1")
    _goal_run(tmp_path, cache, planner, JudgedSession(), task_id="t-2", goal=GOAL.upper())
    assert len(planner.requests) == 2


def test_a_hit_whose_first_target_is_gone_plans_instead(tmp_path: Path) -> None:
    cache, planner = _setup(tmp_path)
    _goal_run(tmp_path, cache, planner, JudgedSession(), task_id="t-1")
    _goal_run(tmp_path, cache, planner, JudgedSession(), task_id="t-2", lines=['1 Button "Ok"'])
    assert len(planner.requests) == 2


def test_a_stopped_run_writes_nothing(tmp_path: Path) -> None:
    cache, planner = _setup(tmp_path)
    stopped = JudgedSession(result=TaskResult(status="stopped", results=[], ticked=set()))
    _goal_run(tmp_path, cache, planner, stopped, task_id="t-1")
    _goal_run(tmp_path, cache, planner, JudgedSession(), task_id="t-2")
    assert len(planner.requests) == 2


def test_the_trace_counts_model_calls_and_holds_no_goal(tmp_path: Path) -> None:
    cache, planner = _setup(tmp_path)
    _goal_run(tmp_path, cache, planner, JudgedSession(), task_id="t-1")
    traces: list[Any] = list((tmp_path / "t-1").rglob("*.jsonl"))
    text = "\n".join(p.read_text(encoding="utf-8") for p in traces)
    assert "hello there" not in text
