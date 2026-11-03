"""Loop rules the runner needs: the screen must not change during an ask (RN-13) and a world
model error is a step result that stops the task, never a traceback (RN-14)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.gate import Ask
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import FakeWorld
from tests.orchestrator.test_edge_cases import _rebuild
from tests.orchestrator.test_loop import Harness, _click, _plan, _send, _type


class _Titled:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world
        self.title = "Amir Khan - Chat"

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        return replace(self.world.observe(hwnd, **kwargs), title=self.title)


class _Dying:
    """Raises on the n-th observe, the way the world model does when the window is gone."""

    def __init__(self, world: FakeWorld, *, dies_at: int) -> None:
        self.world = world
        self.dies_at = dies_at
        self.calls = 0

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        self.calls += 1
        if self.calls >= self.dies_at:
            raise SteerError(ErrorCode.APP_CRASHED, "window 0x10 is gone")
        return self.world.observe(hwnd, **kwargs)


# ---- RN-13: the screen the user approved is the screen acted on ----------------------------


def test_a_title_change_during_the_ask_fails_the_step_without_dispatch() -> None:
    h = Harness()
    titled = _Titled(h.world)
    _rebuild(h, observer=titled)
    h.session.run_step(_type())

    def switch_conversation(_ask: Ask) -> None:
        titled.title = "Sara Ali - Chat"

    h.confirmer.on_ask = switch_conversation
    result = h.session.run_step(_send())
    assert result.ok is False
    assert result.gate == "confirm"
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "screen_changed_during_ask"
    assert h.world.sent == []
    assert h.ledger.admit(h.ledger.key_for(_send())) == "fresh", "the ask is repeated next time"
    assert "Sara" not in "\n".join(h.trace.lines)


def test_the_same_title_after_the_ask_proceeds() -> None:
    h = Harness()
    _rebuild(h, observer=_Titled(h.world))
    h.session.run_step(_type())
    assert h.session.run_step(_send()).ok is True


# ---- RN-14: a world model error is a result, and the task stops -----------------------------


def test_a_window_gone_at_the_first_observe_is_a_step_result() -> None:
    h = Harness()
    _rebuild(h, observer=_Dying(h.world, dies_at=1))
    result = h.session.run_step(_type())
    assert result.ok is False
    assert result.gate == "unreached"
    assert result.code is ErrorCode.APP_CRASHED
    assert result.evidence["detail"] == "observe_raised"
    task = h.session.run_task(_plan(_type(), _send()))
    assert task.status == "stopped"
    assert task.code is ErrorCode.APP_CRASHED


def test_a_window_gone_after_a_confirmed_dispatch_keeps_the_record_open_and_stops() -> None:
    h = Harness()
    h.session.run_step(_type())
    observes_before_verify = len(h.world.observed)
    dying = _Dying(h.world, dies_at=0)
    _rebuild(h, observer=dying)
    # the send: fresh observe (1), post-confirm observe (2), then the verify observe dies
    dying.dies_at = 3
    result = h.session.run_step(_send())
    assert result.ok is False
    assert result.gate == "confirm"
    assert result.code is ErrorCode.APP_CRASHED
    assert result.action is not None, "the action was dispatched before the window went"
    assert len(h.world.observed) > observes_before_verify
    key = h.ledger.key_for(_send())
    assert h.ledger.admit(key) == "confirm", "open record: a confirm next time, never a resend"
    assert h.ledger.records["s3"].phase == "acting"


def test_an_environment_code_stops_the_task_even_after_a_dispatch() -> None:
    h = Harness()
    dying = _Dying(h.world, dies_at=10**9)
    _rebuild(h, observer=dying)
    plan = _plan(_type(), _send(), _click(step_id="s9"))

    def die_on_the_sends_verify(_ask: Ask) -> None:
        dying.dies_at = dying.calls + 2  # the post-confirm observe passes, the verify one dies

    h.confirmer.on_ask = die_on_the_sends_verify
    task = h.session.run_task(plan)
    assert task.status == "stopped"
    assert task.code is ErrorCode.APP_CRASHED
    assert [r.step_id for r in task.results] == ["s2", "s3"]
