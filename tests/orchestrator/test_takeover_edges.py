"""Takeover and kill edges the first pass left untested (TK-03/07/09/10/16/20/24/30): the ask
bracket through the loop with the real core, a kill between the scroll rung and the retry, the
reuse boundary at the after's stamp, a secure step's counts, the key after a refused or an
uncertain kill, a kill or a lost monitor during the wait, the wait's cap at its edges, and the
restart after a wait inside the act stage."""

from __future__ import annotations

import io
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode
from steerai.orchestrator.console import ConsoleConfirmer
from steerai.orchestrator.contracts import Step, StepResult
from steerai.orchestrator.gate import Ask, Gate
from steerai.orchestrator.ledger import LedgerError
from steerai.orchestrator.loop import IDLE_S, MAX_WAIT_S, Session
from steerai.session.monitor import WM_LBUTTONDOWN, MonitorCore
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import COMPOSER, OPTIONS, SEND, FakeLive, FakeWorld, not_delivered
from tests.orchestrator.test_console import _ask
from tests.orchestrator.test_console_kill import KillSwitch
from tests.orchestrator.test_loop import HELLO, Harness, _click, _plan, _send, _type
from tests.orchestrator.test_takeover_loop import FakeMonitor


def _wire(h: Harness, monitor: Any, *, observer: Any = None) -> None:
    h.clock["t"] = 100.0
    h.session = Session(
        task_id="task-1",
        hwnd=0x10,
        observer=h.world if observer is None else observer,
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


@dataclass
class CoreMonitor:
    """The real `MonitorCore` behind the loop's protocol, on the harness clock."""

    clock: dict[str, float]
    armed: bool = False
    core: MonitorCore = field(init=False)

    def __post_init__(self) -> None:
        self.core = MonitorCore(started_at=self.clock["t"] - 10.0)

    def last_user_input(self) -> float:
        return self.core.last_user_input()

    def kill_requested(self) -> bool:
        return self.core.kill_requested()

    def healthy(self) -> bool:
        return self.core.healthy(now=self.clock["t"], thread_alive=True)

    def detail(self) -> str:
        return self.core.detail()

    def maintain(self) -> None:
        return None

    def counts(self) -> dict[str, int]:
        return self.core.counts()

    def mark_ask_open(self) -> None:
        self.core.mark_ask_open()

    def mark_ask_closed(self) -> None:
        self.core.mark_ask_closed()
        if self.armed:
            # the person clicks back to the target window right after answering
            self.armed = False
            self.core.on_mouse(
                message=WM_LBUTTONDOWN, flags=0, extra=0, pt=(5, 5), now=self.clock["t"]
            )

    def key(self) -> None:
        self.core.on_keyboard(flags=0, extra=0, now=self.clock["t"])


class Retitled:
    """The world, with the title swapped once the test says the person switched chats."""

    def __init__(self, world: FakeWorld) -> None:
        self.world = world
        self.title: str | None = None

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        snapshot = self.world.observe(hwnd, **kwargs)
        return snapshot if self.title is None else replace(snapshot, title=self.title)


class SecureComposer:
    """The world, with the composer tagged `secure` as a password field would be."""

    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        snapshot = self.world.observe(hwnd, **kwargs)
        elements = [
            replace(e, secure=True) if e.id == COMPOSER[0] else e for e in snapshot.elements
        ]
        return replace(snapshot, elements=elements)


# ---- TK-07: the answer bracket through the loop -------------------------------------------------


def test_the_typed_answer_is_not_a_takeover_so_the_re_observe_runs_without_a_wait() -> None:
    h = Harness()
    monitor = CoreMonitor(h.clock)
    _wire(h, monitor)
    h.session.run_step(_type())
    h.confirmer.on_ask = lambda _ask: monitor.key()  # the person types the answer
    observed = len(h.world.observed)
    result = h.session.run_step(_send())
    assert result.ok is True, result.to_dict()
    assert "waited_s" not in result.evidence
    assert "restarted" not in result.evidence
    assert "user_input_during_step" not in result.evidence
    assert monitor.counts() == {"answer": 1}
    assert len(h.world.observed) - observed >= 2, "the step's observe and the OR-31 re-observe"
    assert h.world.sent == [HELLO]


def test_input_after_the_answer_waits_for_idle_then_restarts_from_observe() -> None:
    h = Harness()
    monitor = CoreMonitor(h.clock)
    _wire(h, monitor)
    h.session.run_step(_type())
    monitor.armed = True
    result = h.session.run_step(_send())
    assert result.ok is True, result.to_dict()
    assert result.evidence["waited_s"] >= IDLE_S - 0.2
    assert result.evidence["restarted"] is True
    assert monitor.counts() == {"user": 1}
    assert h.world.sent == [HELLO]


def test_the_bracket_closes_when_the_confirmer_raises() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)

    def console_gone(_ask: Ask) -> None:
        raise RuntimeError("console gone")

    h.confirmer.on_ask = console_gone
    with pytest.raises(RuntimeError, match="console gone"):
        h.session.run_step(_send())
    assert monitor.asks == ["open", "closed"]


# ---- TK-10: a kill between the first dispatch and the second ------------------------------------


def test_a_kill_after_the_scroll_rung_stops_before_the_second_dispatch() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    h.semantic.outcomes[OPTIONS[0]] = not_delivered(ErrorCode.ELEMENT_NOT_ACTIONABLE, "offscreen")

    def kill_on_scroll(_executor: Any, action: Any) -> None:
        if action.verb == "scroll_into_view":
            monitor.kill = True

    h.semantic.on_execute = kill_on_scroll
    result = h.session.run_step(_click())
    assert [c[0] for c in h.semantic.calls] == ["click", "scroll_into_view"]
    assert result.gate == "approve", "the gate's own token stays; the kill is the detail"
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"


def test_a_kill_after_a_not_found_attempt_stops_before_the_retry() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    h.semantic.outcomes[OPTIONS[0]] = not_delivered(ErrorCode.ELEMENT_NOT_FOUND, "no_match")
    h.semantic.on_execute = lambda _executor, _action: setattr(monitor, "kill", True)
    result = h.session.run_step(_click())
    assert [c[0] for c in h.semantic.calls] == ["click"]
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"


# ---- TK-16: the reuse boundary is the after's stamp itself --------------------------------------


def test_input_at_the_after_stamp_invalidates_the_reuse_and_input_before_it_does_not() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    assert h.session.run_step(_type()).ok is True
    assert h.session._last_after is not None
    stamp = h.session._last_after[1]
    monitor.last_user = stamp - 1e-6
    first = h.session.run_step(_click(step_id="c1"))
    assert first.evidence["observe_reused"] is True
    assert "reuse_skipped" not in first.evidence
    assert h.session._last_after is not None
    stamp = h.session._last_after[1]
    monitor.last_user = stamp
    second = h.session.run_step(_click(step_id="c2"))
    assert second.evidence["reuse_skipped"] == "user_input"
    assert second.evidence["observe_reused"] is False


# ---- TK-20: a secure step carries the flag but never the counts ---------------------------------


def test_a_secure_step_carries_the_flag_but_no_counts() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor, observer=SecureComposer(h.world))
    h.physical.on_execute = lambda _executor, _action: setattr(
        monitor, "user_events", monitor.user_events + 2
    )
    result = h.session.run_step(_type())
    assert result.evidence["user_input_during_step"] is True
    assert "input_counts" not in result.evidence
    assert "input_counts" not in "\n".join(h.trace.lines)


# ---- TK-09/24: the ledger after a refused and after an uncertain kill ----------------------------


def test_a_kill_the_executor_refused_releases_the_key_for_a_clean_retry() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    h.session.run_step(_type())
    h.semantic.outcomes[SEND[0]] = not_delivered(ErrorCode.GATE_BLOCKED, "killed")
    result = h.session.run_step(_send())
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"
    key = h.ledger.key_for(_send())
    assert h.ledger.outcome_of(key) == "not_delivered"
    assert h.ledger.admit(key) == "fresh"
    del h.semantic.outcomes[SEND[0]]
    again = h.session.run_step(_send())
    assert again.ok is True, again.to_dict()
    assert h.confirmer.asked[-1].previous_attempt is None, "nothing landed: no warning"


def _killed_mid_dispatch(verb: str, mechanism: str) -> ActionResult:
    """The shape `PhysicalExecutor._stopped(killed=True)` returns after a burst went out."""
    return ActionResult(
        verb=verb,
        delivery=Delivery.DELIVERY_UNCERTAIN,
        steps=[ActionStep(label=f"{mechanism}.{verb}", mechanism=mechanism, outcome="failed")],  # type: ignore[arg-type]
        code=ErrorCode.GATE_BLOCKED,
        evidence={"reason": "killed", "phase": "call", "chunks": 1},
    )


def test_a_kill_after_a_partial_delivery_keeps_the_attempt_uncertain_for_a_confirm() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    h.session.run_step(_type())
    h.semantic.outcomes[SEND[0]] = _killed_mid_dispatch("click", "semantic")
    result = h.session.run_step(_send())
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.action is not None, "the action went out: the result keeps it"
    key = h.ledger.key_for(_send())
    assert h.ledger.outcome_of(key) == "delivery_uncertain"
    assert h.ledger.admit(key) == "confirm"
    del h.semantic.outcomes[SEND[0]]
    h.session.run_step(_send())
    assert h.confirmer.asked[-1].previous_attempt == "delivery_uncertain"


def test_a_kill_mid_type_on_the_last_step_ends_the_task_blocked_as_killed() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    h.physical.outcomes[COMPOSER[0]] = _killed_mid_dispatch("type", "physical")
    task = h.session.run_task(_plan(_type()))
    assert task.results[0].evidence.get("detail") == "killed", task.results[0].to_dict()
    assert task.status == "blocked"
    assert task.code is ErrorCode.GATE_BLOCKED
    assert task.evidence["detail"] == "killed"


def test_a_ledger_failure_while_releasing_the_key_after_a_kill_is_a_result_not_a_crash() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    h.session.run_step(_type())
    original = h.ledger.write_ahead

    def write_then_kill(*args: Any, **kwargs: Any) -> Any:
        record = original(*args, **kwargs)
        monitor.kill = True
        return record

    def broken(step: Step, outcome: str) -> None:
        raise LedgerError("outcome could not be written: CryptProtectData failed")

    h.ledger.write_ahead = write_then_kill  # type: ignore[method-assign]  # a timing stand-in
    h.ledger.record_outcome = broken  # type: ignore[method-assign]  # DPAPI gone at that moment
    result = h.session.run_step(_send())
    assert isinstance(result, StepResult)
    assert result.code is ErrorCode.GATE_BLOCKED
    assert h.world.sent == []


# ---- TK-03: a wait inside the act stage restarts on the screen the ask was made on -------------


def test_a_wait_inside_the_act_stage_restarts_on_the_screen_the_ask_was_made_on() -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, last_user=10.0)
    screen = Retitled(h.world)
    _wire(h, monitor, observer=screen)
    h.session.run_step(_type())
    original = h.ledger.mark

    def mark_then_the_person_switches_chats(step: Step, phase: str) -> None:
        original(step, phase)
        if phase == "acting" and screen.title is None:
            monitor.last_user = h.clock["t"]
            screen.title = "Zara Ali - Chat"

    h.ledger.mark = mark_then_the_person_switches_chats  # type: ignore[method-assign]
    result = h.session.run_step(_send())
    assert h.world.sent == [], "the approved thing is not the thing on screen: nothing goes out"
    assert result.ok is False
    assert result.code is ErrorCode.FOCUS_LOST


# ---- TK-25/37: input before the dispatch is not input during the step -----------------------


@dataclass
class ActiveThenIdle(FakeMonitor):
    """Every read while the person is active is one more user event, as the counters would show."""

    def last_user_input(self) -> float:
        now = self.clock["t"]
        if self.input_until is not None and now < self.input_until:
            self.user_events += 1
        return super().last_user_input()


def test_input_that_ended_before_the_dispatch_is_not_input_during_the_step() -> None:
    h = Harness()
    monitor = ActiveThenIdle(h.clock, input_until=101.0)
    _wire(h, monitor)
    result = h.session.run_step(_type())
    assert result.ok is True, result.to_dict()
    assert result.evidence["waited_s"] >= 1.0, "the person was active for a second"
    assert "user_input_during_step" not in result.evidence


# ---- TK-30: the wait polls the kill and the health; the cap is from the boundary ---------------


@dataclass
class KillOnPoll(FakeMonitor):
    kill_at_poll: int = 5

    def maintain(self) -> None:
        super().maintain()
        if self.maintained >= self.kill_at_poll:
            self.kill = True


@dataclass
class DiesOnPoll(FakeMonitor):
    die_at_poll: int = 3

    def maintain(self) -> None:
        super().maintain()
        if self.maintained >= self.die_at_poll:
            self.alive = False


def test_a_kill_during_the_wait_ends_the_task_blocked_without_the_full_wait() -> None:
    h = Harness()
    monitor = KillOnPoll(h.clock, input_until=100.0 + MAX_WAIT_S + 100.0)
    _wire(h, monitor)
    task = h.session.run_task(_plan(_type(), _send()))
    assert task.status == "blocked"
    assert task.evidence["detail"] == "killed"
    assert h.clock["t"] - 100.0 < 1.0
    assert h.physical.calls == []
    assert h.semantic.calls == []


def test_a_monitor_lost_during_the_wait_stops_the_step_before_any_dispatch() -> None:
    h = Harness()
    monitor = DiesOnPoll(h.clock, input_until=100.0 + MAX_WAIT_S + 100.0)
    _wire(h, monitor)
    result = h.session.run_step(_type())
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "monitor_lost"
    assert h.physical.calls == []


@pytest.mark.parametrize(
    ("active_for", "outcome"),
    [(MAX_WAIT_S - 0.15, "ok"), (MAX_WAIT_S + 0.15, "takeover")],
)
def test_the_wait_cap_is_measured_from_the_boundary(active_for: float, outcome: str) -> None:
    h = Harness()
    monitor = FakeMonitor(h.clock, input_until=100.0 + active_for)
    _wire(h, monitor)
    result = h.session.run_step(_type())
    if outcome == "ok":
        assert result.ok is True, result.to_dict()
        assert result.evidence["waited_s"] >= MAX_WAIT_S - 0.5
        assert h.physical.calls != []
    else:
        assert result.code is ErrorCode.GATE_PENDING_USER
        assert result.evidence["detail"] == "takeover"
        assert result.gate == "pending"
        assert h.physical.calls == []


# ---- the console: a typed "killed" is a word, not the switch ------------------------------------


def test_a_typed_killed_is_not_the_kill_token() -> None:
    confirmer = ConsoleConfirmer(
        io.StringIO("killed\n"), io.StringIO(), interactive=True, monitor=KillSwitch()
    )
    assert confirmer.ask(_ask()) == "unanswered"
