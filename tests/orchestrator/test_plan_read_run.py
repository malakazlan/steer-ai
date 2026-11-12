"""The planner's screen read under the kill switch, traced (#233, [v2.80] section 9 row).

The quiet read lasts 2-10 s, so a kill pressed during it must stop the run before the model is
called, and every read's time, end and cap are traced (never its title or lines)."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import Plan
from steerai.orchestrator.loop import TaskResult
from steerai.run import EXIT_BLOCKED, EXIT_USAGE, ScreenRead, run
from tests.orchestrator.test_run import FakeSession, _done
from tests.orchestrator.test_run_goal import (
    SCREEN_LINES,
    FakePlanner,
    _go,
    _options,
    _outcome,
    _plan,
)
from tests.orchestrator.test_run_monitor import FakeMonitor, MonitorWorld
from tests.orchestrator.test_run_replan import _go as _replan_go
from tests.orchestrator.test_run_replan import _two_plan_rig

TITLE = "Amir Khan - Chat - Google Chrome"
KILLED = SteerError(ErrorCode.GATE_BLOCKED, "killed: the kill switch was pressed")


@dataclass
class LatchMonitor(FakeMonitor):
    """The monitor's kill flag latches once pressed (monitor.py)."""

    killed: bool = False

    def kill_requested(self) -> bool:
        return self.killed


@dataclass
class Reader:
    """A screen read that takes `read_s` on the run's clock and records the kill it was given;
    `press` latches the monitor's kill before the read raises `error`."""

    clock: dict[str, float]
    read_s: float = 2.5
    capped: bool = False
    error: Exception | None = None
    press: LatchMonitor | None = None
    kills: list[Callable[[], bool] | None] = field(default_factory=list)

    def __call__(self, _hwnd: int, kill: Callable[[], bool] | None) -> ScreenRead:
        self.kills.append(kill)
        self.clock["t"] += self.read_s
        if self.press is not None:
            self.press.killed = True
        if self.error is not None:
            raise self.error
        return ScreenRead(TITLE, list(SCREEN_LINES), capped=self.capped)


def _goal_run(
    tmp_path: Path, reader: Reader, world: MonitorWorld
) -> tuple[int, dict[str, Any], FakePlanner]:
    outcome = _outcome(_plan())
    planner = FakePlanner(outcome)
    deps = world.deps().replace(screen=reader, planner=planner, monotonic=lambda: reader.clock["t"])
    options = _options(tmp_path, "--trace", str(tmp_path / "t.jsonl"))
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(options, deps, stdin=io.StringIO(_go(outcome)), stdout=stdout, stderr=stderr)
    return code, json.loads(stdout.getvalue().splitlines()[-1]), planner


def _reads(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
    return [r for r in map(json.loads, lines) if r.get("kind") == "plan_read"]


def _world() -> MonitorWorld:
    return MonitorWorld(session=FakeSession(_done(_plan())), monitor=LatchMonitor())


def test_the_plan_read_is_traced_with_its_time_end_and_cap_and_no_screen_text(
    tmp_path: Path,
) -> None:
    reader = Reader({"t": 100.0}, read_s=10.25, capped=True)
    _goal_run(tmp_path, reader, _world())
    assert _reads(tmp_path) == [
        {
            "kind": "plan_read",
            "outcome": "ok",
            "read_ms": 10250,
            "capped": True,
            "lines": len(SCREEN_LINES),
        }
    ]
    text = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert TITLE not in text
    assert "Message" not in text


def test_the_first_read_is_given_the_monitors_kill_switch(tmp_path: Path) -> None:
    reader = Reader({"t": 0.0})
    world = _world()
    _goal_run(tmp_path, reader, world)
    assert reader.kills == [world.monitor.kill_requested]


def test_a_kill_during_the_first_read_blocks_before_the_model_is_called(tmp_path: Path) -> None:
    world = _world()
    assert isinstance(world.monitor, LatchMonitor)
    reader = Reader({"t": 0.0}, read_s=0.5, error=KILLED, press=world.monitor)
    code, out, planner = _goal_run(tmp_path, reader, world)
    assert code == EXIT_BLOCKED
    assert (out["status"], out["detail"], out["code"]) == (
        "blocked",
        "killed",
        ErrorCode.GATE_BLOCKED.value,
    )
    assert planner.requests == []
    assert "session" not in world.calls
    assert "foreground" not in world.calls
    assert world.monitor.closed
    assert _reads(tmp_path) == [
        {"kind": "plan_read", "outcome": "killed", "read_ms": 500, "capped": False, "lines": 0}
    ]


def test_a_gate_blocked_read_with_no_kill_pressed_is_not_called_a_kill(tmp_path: Path) -> None:
    """Principal on #239: only a latched switch makes a GATE_BLOCKED read a kill."""
    reader = Reader({"t": 0.0}, error=KILLED)
    code, out, planner = _goal_run(tmp_path, reader, _world())
    assert code == EXIT_USAGE
    assert out["detail"] == "setup_failed"
    assert planner.requests == []
    assert [r["outcome"] for r in _reads(tmp_path)] == ["raised"]


def test_a_kill_during_a_replan_read_ends_the_task_killed(tmp_path: Path) -> None:
    rig, first, cont = _two_plan_rig(tmp_path)
    monitor = LatchMonitor()
    rig.world = MonitorWorld(monitor=monitor)
    rig.screen_error = KILLED
    session_run = rig.session.run_task

    def run_task(plan: Plan, *, continuation: bool = False) -> TaskResult:
        result = session_run(plan, continuation=continuation)
        monitor.killed = True  # pressed while plan 1 ends, latched for the replan read
        return result

    rig.session.run_task = run_task  # type: ignore[method-assign]
    code, out, _err = rig.run(f"{_replan_go(first)}\n{_replan_go(cont)}\n")
    assert code == EXIT_BLOCKED
    assert (out["status"], out["detail"]) == ("blocked", "killed")
    assert len(rig.planner.requests) == 1, "the continuation is never asked for"


def test_the_replan_read_is_given_the_monitors_kill_switch(tmp_path: Path) -> None:
    rig, first, cont = _two_plan_rig(tmp_path)
    monitor = FakeMonitor()
    rig.world = MonitorWorld(monitor=monitor)
    rig.run(f"{_replan_go(first)}\n{_replan_go(cont)}\n")
    assert rig.kills == [monitor.kill_requested, monitor.kill_requested]
