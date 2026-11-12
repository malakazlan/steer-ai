"""The planner's screen read under the kill switch, traced (#233, [v2.80] section 9 row).

The quiet read lasts 2-10 s, so a kill pressed during it must stop the run before the model is
called, and the read's time and whether it hit the cap are traced (never its title or lines)."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steerai.errors import ErrorCode, SteerError
from steerai.run import EXIT_BLOCKED, ScreenRead, run
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
class Reader:
    """A screen read that takes `read_s` on the run's clock and records the kill it was given."""

    clock: dict[str, float]
    read_s: float = 2.5
    capped: bool = False
    error: Exception | None = None
    kills: list[Callable[[], bool] | None] = field(default_factory=list)

    def __call__(self, _hwnd: int, kill: Callable[[], bool] | None) -> ScreenRead:
        self.kills.append(kill)
        self.clock["t"] += self.read_s
        if self.error is not None:
            raise self.error
        return ScreenRead(TITLE, list(SCREEN_LINES), capped=self.capped)


def _goal_run(
    tmp_path: Path, reader: Reader, world: MonitorWorld
) -> tuple[int, dict[str, Any], FakePlanner]:
    plan = _plan()
    outcome = _outcome(plan)
    planner = FakePlanner(outcome)
    deps = world.deps().replace(screen=reader, planner=planner, monotonic=lambda: reader.clock["t"])
    options = _options(tmp_path, "--trace", str(tmp_path / "t.jsonl"))
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(options, deps, stdin=io.StringIO(_go(outcome)), stdout=stdout, stderr=stderr)
    return code, json.loads(stdout.getvalue().splitlines()[-1]), planner


def _records(tmp_path: Path, kind: str) -> list[dict[str, Any]]:
    lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
    return [r for r in map(json.loads, lines) if r.get("kind") == kind]


def test_the_plan_read_is_traced_with_its_time_and_cap_and_no_screen_text(tmp_path: Path) -> None:
    reader = Reader({"t": 100.0}, read_s=10.25, capped=True)
    world = MonitorWorld(session=FakeSession(_done(_plan())))
    _goal_run(tmp_path, reader, world)
    assert _records(tmp_path, "plan_read") == [
        {"kind": "plan_read", "read_ms": 10250, "capped": True, "lines": len(SCREEN_LINES)}
    ]
    text = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert TITLE not in text
    assert "Message" not in text


def test_the_first_read_is_given_the_monitors_kill_switch(tmp_path: Path) -> None:
    reader = Reader({"t": 0.0})
    world = MonitorWorld(session=FakeSession(_done(_plan())))
    _goal_run(tmp_path, reader, world)
    assert reader.kills == [world.monitor.kill_requested]


def test_a_kill_during_the_first_read_blocks_before_the_model_is_called(tmp_path: Path) -> None:
    reader = Reader({"t": 0.0}, error=KILLED)
    world = MonitorWorld(session=FakeSession(_done(_plan())))
    code, out, planner = _goal_run(tmp_path, reader, world)
    assert code == EXIT_BLOCKED
    assert (out["status"], out["detail"], out["code"]) == (
        "blocked",
        "killed",
        ErrorCode.GATE_BLOCKED.value,
    )
    assert planner.requests == []
    assert "session" not in world.calls


def test_a_kill_during_a_replan_read_ends_the_task_killed(tmp_path: Path) -> None:
    rig, first, cont = _two_plan_rig(tmp_path)
    rig.screen_error = KILLED
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
