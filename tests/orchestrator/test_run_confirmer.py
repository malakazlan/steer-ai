"""The runner takes its confirmer from `Deps.make_confirmer` (packet launcher-2026-11-04):
the console by default, the overlay's broker from the app. Both construction sites (the plan
preview's `go` ask and the session's `Bound`) use the factory."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steerai.orchestrator.gate import Ask
from steerai.run import EXIT_DONE, ScreenRead, run
from tests.orchestrator.test_run import FakeSession, World, _done, _options, _run
from tests.orchestrator.test_run_goal import FakePlanner, _outcome, _plan


@dataclass
class RecordingConfirmer:
    asks: list[Ask] = field(default_factory=list)
    go_ids: list[str] = field(default_factory=list)
    made_with: tuple[Any, Any, Any] | None = None

    def ask(self, ask: Ask) -> str:
        self.asks.append(ask)
        return f"yes:{ask.decision_id}"

    def ask_go(self, go_id: str) -> str:
        self.go_ids.append(go_id)
        return "go"


def test_the_default_confirmer_is_still_the_console(tmp_path: Path) -> None:
    from tests.orchestrator.test_loop import _type  # noqa: PLC0415

    plan = _plan()
    world = World(session=FakeSession(_done(plan)))
    _run(_options(tmp_path, plan), world, stdin="no\n")
    assert type(world.bound[0].confirmer).__name__ == "ConsoleConfirmer"
    assert _type is not None


def test_a_made_confirmer_answers_the_preview_and_reaches_the_session(tmp_path: Path) -> None:
    plan = _plan()
    outcome = _outcome(plan)
    world = World(session=FakeSession(_done(plan)))
    made: list[RecordingConfirmer] = []

    def make(stdin: Any, stderr: Any, monitor: Any) -> RecordingConfirmer:
        confirmer = RecordingConfirmer(made_with=(stdin, stderr, monitor))
        made.append(confirmer)
        return confirmer

    def screen(_hwnd: int, _kill: Callable[[], bool] | None) -> ScreenRead:
        lines = ['1  Edit "Message"   [0,0,10,10]']
        return ScreenRead("Amir Khan - Chat - Google Chrome", lines, capped=False)

    argv = [
        "--goal",
        "send hello there to Amir",
        "--window",
        "chrome",
        "--ledger-dir",
        str(tmp_path / "l"),
        "--task-id",
        "goal-1",
    ]
    from steerai.run import parse_args  # noqa: PLC0415

    options = parse_args(argv)
    deps = world.deps().replace(screen=screen, planner=FakePlanner(outcome), make_confirmer=make)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(options, deps, stdin=io.StringIO(""), stdout=stdout, stderr=stderr)
    assert code == EXIT_DONE, stderr.getvalue()
    assert json.loads(stdout.getvalue())["status"] == "done"
    assert len(made) == 1, "one confirmer for the preview and the session"
    assert made[0].go_ids == [hashlib.sha256(outcome.data + b"goal-1").hexdigest()[:12]]
    assert world.bound[0].confirmer is made[0]
    assert made[0].made_with is not None
    assert made[0].made_with[1] is stderr
