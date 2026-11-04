"""The loop with a takeover monitor (TK-03/09/16/22/24/29/30/31): wait for idle at every
boundary, restart a step whose wait ended after its observe, route a kill to `blocked`, stop
on a lost monitor, and carry only counts in the evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from steerai.errors import ErrorCode
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.loop import IDLE_S, MAX_WAIT_S, Session
from tests.orchestrator.fakes import FakeExecutor, FakeLive, not_delivered
from tests.orchestrator.test_loop import HELLO, Harness, _click, _plan, _send, _type


@dataclass
class FakeMonitor:
    clock: dict[str, float]
    last_user: float = 0.0
    kill: bool = False
    alive: bool = True
    maintained: int = 0
    asks: list[str] = field(default_factory=list)
    user_events: int = 0
    other_events: int = 0
    steer_events: int = 0
    input_until: float | None = None
    """While set, every read of `last_user_input` reports input "just now" until this instant."""

    def last_user_input(self) -> float:
        now = self.clock["t"]
        if self.input_until is not None and now < self.input_until:
            return now
        return self.last_user

    def kill_requested(self) -> bool:
        return self.kill

    def healthy(self) -> bool:
        return self.alive

    def detail(self) -> str:
        return "monitor_lost"

    def maintain(self) -> None:
        self.maintained += 1

    def counts(self) -> dict[str, int]:
        return {
            "user": self.user_events,
            "injected_other": self.other_events,
            "steer": self.steer_events,
        }

    def mark_ask_open(self) -> None:
        self.asks.append("open")

    def mark_ask_closed(self) -> None:
        self.asks.append("closed")


def _wire(h: Harness, monitor: FakeMonitor) -> None:
    from steerai.orchestrator.gate import Gate  # noqa: PLC0415

    h.session = Session(
        task_id="task-1",
        hwnd=0x10,
        observer=h.world,
        semantic=h.semantic,
        physical=h.physical,
        gate=Gate(confirmer=h.confirmer, own_pid=4242, task_id="task-1"),
        ledger=h.ledger,
        live=FakeLive(h.world),
        trace=h.trace,
        monitor=monitor,
        monotonic=lambda: h.clock["t"],
        sleep=lambda s: h.clock.__setitem__("t", h.clock["t"] + s),
        verify_deadline_s=0.5,
    )


# ---- waiting for idle ------------------------------------------------------------------------


def test_a_step_waits_until_the_person_has_been_idle_before_it_starts() -> None:
    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=99.5)
    _wire(h, monitor)
    result = h.session.run_step(_type())
    assert result.ok is True
    assert h.clock["t"] >= 99.5 + IDLE_S
    assert result.evidence["waited_s"] >= IDLE_S - 0.5
    assert monitor.maintained >= 1, "the self-probe is driven from the boundary"


def test_no_recent_input_means_no_wait() -> None:
    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    before = h.clock["t"]
    result = h.session.run_step(_type())
    assert result.ok is True
    assert "waited_s" not in result.evidence
    assert h.clock["t"] - before < 1.0


def test_continuous_input_pauses_the_task_as_a_takeover_with_nothing_dispatched() -> None:
    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, input_until=100.0 + MAX_WAIT_S + 1000)
    _wire(h, monitor)
    task = h.session.run_task(_plan(_type(), _send()))
    assert task.status == "paused"
    assert task.code is ErrorCode.GATE_PENDING_USER
    assert task.evidence["detail"] == "takeover"
    assert h.semantic.calls == []
    assert h.physical.calls == []
    assert h.clock["t"] >= 100.0 + MAX_WAIT_S


def test_a_wait_that_ends_after_the_observe_restarts_the_step_from_observe() -> None:
    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    observed_before = len(h.world.observed)

    def person_touches_during_the_gate(_ask: Ask) -> None:
        monitor.last_user = h.clock["t"]  # input between the observe and the dispatch

    h.confirmer.on_ask = person_touches_during_the_gate
    h.session.run_step(_type())
    result = h.session.run_step(_send())
    assert result.ok is True
    assert result.evidence["restarted"] is True
    assert h.clock["t"] >= monitor.last_user + IDLE_S
    # the send observed once fresh, again after the ask (OR-31), and again after the wait
    assert len(h.world.observed) - observed_before >= 4


def test_user_input_newer_than_the_decided_after_invalidates_the_reuse() -> None:
    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    first = h.session.run_step(_type("t1"))
    assert first.ok is True
    monitor.last_user = h.clock["t"]  # the person types right after our step
    h.clock["t"] += IDLE_S + 0.1
    second = h.session.run_step(_click(step_id="c1"))
    assert second.evidence["observe_reused"] is False
    assert second.evidence["reuse_skipped"] == "user_input"


# ---- the kill switch ---------------------------------------------------------------------------


def test_a_kill_before_the_step_is_blocked_with_nothing_dispatched() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, kill=True)
    _wire(h, monitor)
    task = h.session.run_task(_plan(_type(), _send()))
    assert task.status == "blocked"
    assert task.code is ErrorCode.GATE_BLOCKED
    assert task.evidence["detail"] == "killed"
    assert task.results[0].gate == "unreached", "the gate never ran; its token is not rewritten"
    assert h.physical.calls == []


def test_a_kill_between_the_record_and_the_dispatch_releases_the_key() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock)
    _wire(h, monitor)
    h.session.run_step(_type())
    original = h.ledger.write_ahead

    def write_then_kill(*args: Any, **kwargs: Any) -> Any:
        record = original(*args, **kwargs)
        monitor.kill = True  # the hot key lands between the record and the dispatch
        return record

    h.ledger.write_ahead = write_then_kill  # type: ignore[method-assign]  # a timing stand-in
    result = h.session.run_step(_send())
    assert result.ok is False
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"
    assert h.world.sent == []
    key = h.ledger.key_for(_send())
    assert h.ledger.outcome_of(key) == "not_delivered", "nothing sent: the key is released"
    assert h.ledger.admit(key) == "fresh"


def test_a_kill_reported_by_the_executor_blocks_the_task_and_keeps_the_action() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock)
    _wire(h, monitor)
    h.session.run_step(_type())

    def killed_mid_dispatch(executor: FakeExecutor, action: Any) -> None:
        executor.outcomes[action.element_id] = not_delivered(ErrorCode.GATE_BLOCKED, "killed")

    h.semantic.on_execute = killed_mid_dispatch
    task = h.session.run_task(_plan(_send(), _click(step_id="c9")))
    assert task.status == "blocked"
    assert task.evidence["detail"] == "killed"
    assert [r.step_id for r in task.results] == ["s3"]
    assert task.results[0].action is not None


# ---- a lost monitor ----------------------------------------------------------------------------


def test_an_unhealthy_monitor_stops_the_task_before_any_dispatch() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, alive=False)
    _wire(h, monitor)
    task = h.session.run_task(_plan(_type(), _send()))
    assert task.status == "stopped"
    assert task.code is ErrorCode.GATE_BLOCKED
    assert task.evidence["detail"] == "monitor_lost"
    assert h.physical.calls == []


# ---- evidence ----------------------------------------------------------------------------------


def test_input_during_a_step_is_evidence_as_counts_only() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)

    def person_moves_during_the_type(executor: FakeExecutor, _action: Any) -> None:
        monitor.user_events += 3
        monitor.other_events += 1
        monitor.steer_events += 9

    h.physical.on_execute = person_moves_during_the_type
    result = h.session.run_step(_type())
    assert result.ok is True, "the verifier's verdict stands (TK-25)"
    assert result.evidence["user_input_during_step"] is True
    assert result.evidence["input_counts"] == {"user": 3, "injected_other": 1}
    assert "steer" not in str(result.evidence["input_counts"])
    assert HELLO not in "\n".join(h.trace.lines)


def test_no_input_during_a_step_leaves_no_flag() -> None:
    h = Harness()
    _wire(h, FakeMonitor(h.clock, last_user=10.0))
    result = h.session.run_step(_type())
    assert "user_input_during_step" not in result.evidence
    assert "input_counts" not in result.evidence


def test_without_a_monitor_nothing_changes() -> None:
    h = Harness()
    result = h.session.run_step(_type())
    assert result.ok is True
    assert "waited_s" not in result.evidence
    assert "user_input_during_step" not in result.evidence


# ---- the principal's implementation review ---------------------------------------------------


class _Titled:
    def __init__(self, world: Any) -> None:
        self.world = world
        self.title = "Amir Khan - Chat"

    def observe(self, hwnd: int, **kwargs: Any) -> Any:
        from dataclasses import replace  # noqa: PLC0415

        return replace(self.world.observe(hwnd, **kwargs), title=self.title)


def test_a_wait_inside_the_act_loop_restarts_with_the_title_check() -> None:
    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    titled = _Titled(h.world)
    _wire(h, monitor)
    h.session._observer = titled  # the test swaps the observer behind the session
    h.session.run_step(_type())
    original = h.ledger.write_ahead

    def person_switches_chat_after_the_record(*args: Any, **kwargs: Any) -> Any:
        record = original(*args, **kwargs)
        monitor.last_user = h.clock["t"]
        titled.title = "Sara Ali - Chat"
        return record

    h.ledger.write_ahead = person_switches_chat_after_the_record  # type: ignore[method-assign]
    result = h.session.run_step(_send())
    assert result.ok is False
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "screen_changed_during_wait"
    assert h.world.sent == []
    key = h.ledger.key_for(_send())
    assert h.ledger.outcome_of(key) == "not_delivered", "nothing sent after the record"


def test_a_takeover_after_the_record_leaves_no_open_record() -> None:
    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    h.session.run_step(_type())
    original = h.ledger.write_ahead

    def person_keeps_typing_after_the_record(*args: Any, **kwargs: Any) -> Any:
        record = original(*args, **kwargs)
        monitor.input_until = h.clock["t"] + MAX_WAIT_S + 1000
        return record

    h.ledger.write_ahead = person_keeps_typing_after_the_record  # type: ignore[method-assign]
    result = h.session.run_step(_send())
    assert result.gate == "pending"
    assert result.evidence["detail"] == "takeover"
    assert h.world.sent == []
    key = h.ledger.key_for(_send())
    assert h.ledger.outcome_of(key) == "not_delivered"
    assert h.ledger.admit(key) == "fresh", "a resume asks afresh with no warning"
