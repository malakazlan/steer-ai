"""Follow-ups from the takeover PR's final pass: a depth-counted ask bracket, no restart after a
timed-out close, a boundary before the scroll dispatch, one shared idle rule."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.loop import IDLE_S, MAX_WAIT_S, wait_for_idle
from steerai.session.monitor import HookMonitor, MonitorCore
from tests.orchestrator.fakes import FakeExecutor, not_delivered
from tests.orchestrator.test_loop import Harness, _click, _type
from tests.orchestrator.test_takeover_loop import FakeMonitor, _wire


def test_the_ask_bracket_counts_depth_so_two_owners_cannot_close_each_other() -> None:
    core = MonitorCore(started_at=100.0)
    core.mark_ask_open()  # the loop
    core.mark_ask_open()  # the console
    core.mark_ask_closed()  # the console returns first
    core.on_keyboard(flags=0, extra=0, now=101.0)
    assert core.last_user_input() == 100.0, "still inside the loop's bracket: an answer"
    core.mark_ask_closed()
    core.on_keyboard(flags=0, extra=0, now=102.0)
    assert core.last_user_input() == 102.0
    core.mark_ask_closed()  # a stray close never goes negative
    core.mark_ask_open()
    core.on_keyboard(flags=0, extra=0, now=103.0)
    assert core.last_user_input() == 102.0


def test_a_monitor_whose_close_timed_out_refuses_to_start_again() -> None:
    monitor = HookMonitor()
    monitor._stop_timed_out = True  # the old thread may still hold the callbacks
    with pytest.raises(SteerError) as info:
        monitor.start()
    assert info.value.code is ErrorCode.GATE_BLOCKED
    assert "monitor_unavailable" in info.value.message


def test_a_kill_between_the_first_attempt_and_the_scroll_rung_stops_before_the_scroll() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    h.session.run_step(_type())

    def offscreen_then_kill(executor: FakeExecutor, action: Any) -> None:
        if action.verb == "click":
            executor.outcomes[action.element_id] = not_delivered(
                ErrorCode.ELEMENT_NOT_ACTIONABLE, "offscreen"
            )
            monitor.kill = True

    h.semantic.on_execute = offscreen_then_kill
    result = h.session.run_step(_click(risk="R1"))
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"
    assert [c[0] for c in h.semantic.calls] == ["click"], "no scroll dispatch after the key"


def test_the_shared_idle_rule_returns_the_tokens_the_runner_and_the_loop_map() -> None:
    clock = {"t": 100.0}

    def sleep(s: float) -> None:
        clock["t"] += s

    monitor = FakeMonitor(clock, last_user=10.0)
    assert wait_for_idle(monitor, monotonic=lambda: clock["t"], sleep=sleep) is None
    monitor.kill = True
    assert wait_for_idle(monitor, monotonic=lambda: clock["t"], sleep=sleep) == "killed"
    monitor.kill = False
    monitor.alive = False
    assert wait_for_idle(monitor, monotonic=lambda: clock["t"], sleep=sleep) == "monitor_lost"
    monitor.alive = True
    monitor.last_user = clock["t"] - IDLE_S + 0.5
    assert wait_for_idle(monitor, monotonic=lambda: clock["t"], sleep=sleep) is None
    assert clock["t"] >= monitor.last_user + IDLE_S
    monitor.input_until = clock["t"] + MAX_WAIT_S + 100
    assert wait_for_idle(monitor, monotonic=lambda: clock["t"], sleep=sleep) == "takeover"
