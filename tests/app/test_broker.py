"""The ask broker (packet launcher-2026-11-04, design 5.1 asks): the runner thread blocks on an
ask, the UI thread answers with a typed `Answer`, and the broker yields exactly the bound tokens
the gate accepts. Pure: the clock, the sleep and the kill source are injected."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from steerai.app.broker import Answer, AskBroker, PendingAsk
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.identity import CandidateLine


@dataclass
class FakeKill:
    kill: bool = False
    opened: int = 0
    closed: int = 0

    def kill_requested(self) -> bool:
        return self.kill

    def mark_ask_open(self) -> None:
        self.opened += 1

    def mark_ask_closed(self) -> None:
        self.closed += 1


@dataclass
class Clock:
    t: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _ask(risk: str = "R2", kind: str = "yes", candidates: int = 0) -> Ask:
    lines = tuple(
        CandidateLine(index=i + 1, role="ListItem", name_part=f"Amir {i}", preview="hi", tags=())
        for i in range(candidates)
    )
    return Ask(
        decision_id="d-1",
        task_id="t",
        action_key="k",
        verb="send",
        target_role="Button",
        risk=risk,  # type: ignore[arg-type]
        expect={},
        params={"text": "hello"},
        target_name="Send",
        kind=kind,  # type: ignore[arg-type]
        candidates=lines,
    )


def _broker(kill: FakeKill | None = None, clock: Clock | None = None) -> tuple[AskBroker, Clock]:
    clock = clock or Clock()
    broker = AskBroker(monitor=kill, sleep=clock.sleep, poll_s=0.05)
    return broker, clock


def _answer_later(broker: AskBroker, answer: Answer, *, after_polls: int, clock: Clock) -> None:
    """Post the answer from another thread once the broker has polled a few times."""

    def post() -> None:
        while len(clock.sleeps) < after_polls:
            pass
        broker.answer(answer)

    threading.Thread(target=post, daemon=True).start()


def test_yes_for_the_shown_ask_is_the_bound_token() -> None:
    kill = FakeKill()
    broker, clock = _broker(kill)
    _answer_later(broker, Answer.yes("d-1"), after_polls=2, clock=clock)
    assert broker.confirm(_ask()) == "yes:d-1"
    assert (kill.opened, kill.closed) == (1, 1), "the ask bracket wraps the wait"
    assert broker.pending() is None


def test_yes_for_a_stale_id_never_counts() -> None:
    broker, clock = _broker()
    _answer_later(broker, Answer.yes("d-other"), after_polls=1, clock=clock)
    _later(clock, 3, lambda: broker.answer(Answer.no()))
    assert broker.confirm(_ask()) == "no"


def _wait_polls(clock: Clock, n: int) -> None:
    while len(clock.sleeps) < n:
        pass


def _later(clock: Clock, polls: int, action: Callable[[], object]) -> None:
    """Run `action` on another thread once the broker has polled `polls` times."""

    def run() -> None:
        _wait_polls(clock, polls)
        action()

    threading.Thread(target=run, daemon=True).start()


def test_no_is_no_and_a_choice_is_a_bound_ordinal() -> None:
    broker, clock = _broker()
    _answer_later(broker, Answer.no(), after_polls=1, clock=clock)
    assert broker.confirm(_ask()) == "no"
    broker, clock = _broker()
    _answer_later(broker, Answer.choose(2, "d-1"), after_polls=1, clock=clock)
    assert broker.confirm(_ask(kind="which", candidates=3)) == "2:d-1"


def test_a_choice_out_of_range_is_unanswered_until_a_valid_one() -> None:
    broker, clock = _broker()
    _answer_later(broker, Answer.choose(4, "d-1"), after_polls=1, clock=clock)
    _later(clock, 3, lambda: broker.answer(Answer.choose(1, "d-1")))
    assert broker.confirm(_ask(kind="which", candidates=3)) == "1:d-1"


def test_the_kill_switch_ends_the_wait_with_killed() -> None:
    kill = FakeKill()
    broker, clock = _broker(kill)
    _later(clock, 2, lambda: setattr(kill, "kill", True))
    assert broker.confirm(_ask()) == "killed"
    assert kill.closed == 1


def test_no_ask_times_out_it_waits_for_the_person_or_the_kill() -> None:
    """Schema gate 2026-11-04: no timeout in this slice; closing the window is the kill."""
    broker, clock = _broker()
    _later(clock, 400, lambda: broker.answer(Answer.no()))
    assert broker.confirm(_ask(risk="R0")) == "no"
    assert len(clock.sleeps) >= 400


def test_go_only_for_the_shown_id() -> None:
    broker, clock = _broker()
    _answer_later(broker, Answer.go("other"), after_polls=1, clock=clock)
    _later(clock, 3, lambda: broker.answer(Answer.go("abc123")))
    assert broker.go("abc123") == "go"


def test_the_pending_ask_is_visible_to_the_ui_while_it_waits() -> None:
    broker, clock = _broker()
    seen: list[PendingAsk | None] = []

    def peek() -> None:
        _wait_polls(clock, 1)
        seen.append(broker.pending())
        broker.answer(Answer.no())

    threading.Thread(target=peek, daemon=True).start()
    broker.confirm(_ask())
    assert seen
    assert seen[0] is not None
    assert seen[0].kind == "yes"
    assert seen[0].decision_id == "d-1"
    assert "yes:d-1" not in seen[0].text, "the overlay shows buttons, not tokens to type"


def test_an_answer_with_no_pending_ask_is_dropped() -> None:
    broker, _ = _broker()
    broker.answer(Answer.yes("d-1"))  # nothing waits: must not raise or stick
    assert broker.pending() is None


@pytest.mark.parametrize("bad", [0, -1])
def test_choose_rejects_non_positive_ordinals(bad: int) -> None:
    with pytest.raises(ValueError, match="ordinal"):
        Answer.choose(bad, "d-1")


def test_two_confirms_at_once_are_refused() -> None:
    """One task, one ask at a time (5.1)."""
    broker, clock = _broker()
    started = threading.Event()

    def first() -> None:
        started.set()
        broker.confirm(_ask())

    threading.Thread(target=first, daemon=True).start()
    started.wait(1.0)
    _wait_polls(clock, 1)
    with pytest.raises(RuntimeError):
        broker.confirm(_ask())
    broker.answer(Answer.no())
