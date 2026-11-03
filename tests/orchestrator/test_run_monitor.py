"""The runner with the takeover monitor (TK-13/18/22/36): started after the ledger and before
any window work, closed on every exit path, handed to the session, the kill key validated."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.loop import TaskResult
from steerai.run import (
    EXIT_BLOCKED,
    EXIT_ERROR,
    EXIT_PAUSED,
    EXIT_USAGE,
    Options,
    UsageError,
    parse_args,
    run,
    warm_up,
)
from tests.orchestrator.fakes import build
from tests.orchestrator.test_loop import _type
from tests.orchestrator.test_run import FakeSession, World, _done, _options, _plan


@dataclass
class FakeMonitor:
    closed: bool = False
    options: Options | None = None
    fail: str | None = None

    def kill_requested(self) -> bool:
        return False

    def last_user_input(self) -> float:
        return 0.0

    def healthy(self) -> bool:
        return True

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
        self.closed = True


@dataclass
class MonitorWorld(World):
    monitor: FakeMonitor = field(default_factory=FakeMonitor)
    start_error: Exception | None = None

    def deps(self) -> Any:
        base = super().deps()

        def start(options: Options) -> FakeMonitor:
            self.calls.append("monitor")
            if self.start_error is not None:
                raise self.start_error
            self.monitor.options = options
            return self.monitor

        return base.replace(start_monitor=start)


def _run(options: Options, world: MonitorWorld) -> tuple[int, dict[str, Any], str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(options, world.deps(), stdin=io.StringIO(), stdout=stdout, stderr=stderr)
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    return code, json.loads(lines[0]), stderr.getvalue()


def test_the_monitor_starts_after_the_ledger_before_any_window_work_and_reaches_the_session(
    tmp_path: Path,
) -> None:
    plan = _plan(_type())
    world = MonitorWorld(session=FakeSession(_done(plan)))
    code, _, _ = _run(_options(tmp_path, plan), world)
    assert code == 0
    assert world.calls == ["ledger", "monitor", "list_windows", "foreground", "session"]
    assert world.bound[0].monitor is world.monitor
    assert world.monitor.closed is True
    assert world.monitor.options is not None
    assert world.monitor.options.kill_key == "ctrl+alt+shift+s"


def test_a_monitor_that_cannot_start_is_a_refusal_before_any_window_work(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = MonitorWorld(
        session=FakeSession(_done(plan)),
        start_error=SteerError(
            ErrorCode.GATE_BLOCKED, "kill_key_unavailable: RegisterHotKey failed"
        ),
    )
    code, out, err = _run(_options(tmp_path, plan), world)
    assert code == EXIT_USAGE
    assert out["code"] == "gate_blocked"
    assert out["detail"] == "kill_key_unavailable"
    assert "--kill-key" in err
    assert "list_windows" not in world.calls
    assert world.ledger is not None
    assert world.ledger.closed is True


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [(RuntimeError("boom"), EXIT_ERROR), (KeyboardInterrupt(), EXIT_PAUSED)],
)
def test_the_monitor_is_closed_on_every_exit_path(
    tmp_path: Path, outcome: BaseException, exit_code: int
) -> None:
    plan = _plan(_type())
    world = MonitorWorld(session=FakeSession(outcome))
    code, _, _ = _run(_options(tmp_path, plan), world)
    assert code == exit_code
    assert world.monitor.closed is True


def test_a_killed_task_exits_3_with_the_token(tmp_path: Path) -> None:
    plan = _plan(_type())
    killed = TaskResult(
        status="blocked",
        results=[],
        ticked=set(),
        code=ErrorCode.GATE_BLOCKED,
        evidence={"detail": "killed"},
    )
    world = MonitorWorld(session=FakeSession(killed))
    code, out, _ = _run(_options(tmp_path, plan), world)
    assert code == EXIT_BLOCKED
    assert out["detail"] == "killed"
    assert world.monitor.closed is True


def test_the_kill_key_is_validated_on_the_command_line(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    options = parse_args(["--plan", str(plan), "--window", "x", "--kill-key", "ctrl+alt+k"])
    assert options.kill_key == "ctrl+alt+k"
    for bad in ("s", "f12", "alt+f4", "ctrl+v"):
        with pytest.raises(UsageError):
            parse_args(["--plan", str(plan), "--window", "x", "--kill-key", bad])


def test_warm_up_stops_on_a_kill_before_the_cap() -> None:
    clock = {"t": 0.0}

    def observe() -> Any:
        return build([(0, "Pane", "p", (42, 1), None)], snapshot_id="s", taken_at=clock["t"])

    def sleep(s: float) -> None:
        clock["t"] += s

    with pytest.raises(SteerError) as info:
        warm_up(
            observe,
            cap_s=10.0,
            monotonic=lambda: clock["t"],
            sleep=sleep,
            kill=lambda: clock["t"] >= 0.5,
        )
    assert info.value.code is ErrorCode.GATE_BLOCKED
    assert "killed" in info.value.message
    assert clock["t"] < 2.0
