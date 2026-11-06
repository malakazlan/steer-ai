"""A locked session is never acted on (issue #31): the Windows 10 lock screen runs as
`LockApp.exe` on the default desktop, so the secure-desktop check never fired and five live runs
went on behind it. The session's lock flag (`WTSINFOEX_LEVEL1.SessionFlags`) stops the task as
`secure_desktop` / `session_locked`; an unknown state stops too (unclear means fail)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.run import EXIT_USAGE, run
from tests.orchestrator.test_loop import Harness, _plan, _type
from tests.orchestrator.test_run import FakeSession, World, _done, _options
from tests.orchestrator.test_run import _plan as _file_plan


@pytest.mark.parametrize("state", [True, None])
def test_a_locked_or_unknown_session_stops_the_task_before_any_action(state: bool | None) -> None:
    h = Harness()
    h.session._locked = lambda: state
    result = h.session.run_task(_plan(_type()))
    assert result.status == "stopped"
    assert result.code is ErrorCode.SECURE_DESKTOP
    assert result.evidence["detail"] == "session_locked"
    assert h.semantic.calls == []
    assert h.physical.calls == []


def test_an_unlocked_session_runs() -> None:
    h = Harness()
    h.session._locked = lambda: False
    result = h.session.run_task(_plan(_type()))
    assert result.evidence.get("detail") != "session_locked"
    assert h.semantic.calls or h.physical.calls


def test_the_lock_is_read_at_every_step_boundary() -> None:
    """Locked after the first step: the second never acts."""
    h = Harness()
    h.session._locked = lambda: bool(h.semantic.calls or h.physical.calls)  # locks once acted
    result = h.session.run_task(_plan(_type(), _type(step_id="s4")))
    assert result.evidence["detail"] == "session_locked"
    assert [r.step_id for r in result.results][-1] == "s4"
    assert result.results[-1].action is None


def test_the_runner_refuses_a_locked_session_before_the_foreground_tap(tmp_path: Path) -> None:
    plan = _file_plan(_type())
    world = World(session=FakeSession(_done(plan)))
    deps = world.deps().replace(session_locked=lambda: True)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(_options(tmp_path, plan), deps, stdin=io.StringIO(""), stdout=stdout, stderr=stderr)
    out: dict[str, Any] = json.loads(stdout.getvalue())
    assert code == EXIT_USAGE
    assert (out["code"], out["detail"]) == ("secure_desktop", "session_locked")
    assert world.foreground_calls == []
    assert "session" not in world.calls
    assert "locked" in stderr.getvalue()


def test_a_lock_during_the_idle_wait_stops_before_the_action() -> None:
    """PR #33 review item 1: Win+L is input, so it starts the idle wait at the boundary right
    before the dispatch; the lock comes while that wait runs and is read again after it."""
    from tests.orchestrator.test_loop import _send  # noqa: PLC0415
    from tests.orchestrator.test_takeover_loop import FakeMonitor, _wire  # noqa: PLC0415

    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    locks_at: list[float] = []
    mark = h.ledger.mark

    def win_l_right_before_the_act(step: Any, phase: Any) -> None:
        mark(step, phase)
        if phase == "acting" and not locks_at:
            monitor.last_user = h.clock["t"]  # the key press is input: the act's wait starts
            locks_at.append(h.clock["t"] + 0.5)  # the lock screen lands while it runs

    h.ledger.mark = win_l_right_before_the_act  # type: ignore[method-assign]
    h.session._locked = lambda: bool(locks_at) and h.clock["t"] >= locks_at[0]
    h.session.run_step(_type())
    sent_before = list(h.world.sent)
    result = h.session.run_step(_send())
    assert result.evidence["detail"] == "session_locked"
    assert result.action is None
    assert h.world.sent == sent_before


@pytest.mark.parametrize(
    ("state", "flags", "expected"),
    [
        (0, 0, True),  # WTSActive, WTS_SESSIONSTATE_LOCK
        (0, 1, False),  # WTSActive, WTS_SESSIONSTATE_UNLOCK (measured on this console)
        (0, -1, None),  # WTS_SESSIONSTATE_UNKNOWN (0xFFFFFFFF as a LONG)
        (4, 1, None),  # WTSDisconnected: not a session anyone sees
        (1, 1, None),  # WTSConnected, not active
    ],
)
def test_the_lock_read_needs_an_active_session(
    state: int, flags: int, expected: bool | None
) -> None:
    """PR #33 review item 4: the connection state is read too (WTS_CONNECTSTATE_CLASS)."""
    from steerai.probes.windows import lock_state  # noqa: PLC0415

    assert lock_state(session_state=state, session_flags=flags) is expected
