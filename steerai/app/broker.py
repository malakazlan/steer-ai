"""The ask broker: the thread seam between the runner and the overlay (design 5.1 asks;
packet launcher-2026-11-04).

The runner thread blocks in `confirm` / `go`; the UI thread posts a typed `Answer`. The broker
produces exactly the bound tokens the gate and the runner accept (`yes:<id>`, `no`, `<n>:<id>`,
`go`), never anything else: a stale decision id, an out-of-range ordinal or a mismatched go id
is dropped and the wait continues. The kill switch is polled beside the wait, like the console.
No ask times out (schema gate 2026-11-04: the gate auto-approves R0/R1, so the only R0/R1 ask is
the identity ask, and a GUI pause would be a worse wait than the person's answer); an ask ends
with the person's answer or the kill, and closing the window is the kill.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from steerai.orchestrator.console import render_ask
from steerai.orchestrator.gate import Ask

KILL_POLL_S = 0.1


class KillSource(Protocol):
    def kill_requested(self) -> bool: ...
    def mark_ask_open(self) -> None: ...
    def mark_ask_closed(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Answer:
    """What the UI posts: typed, never free text."""

    kind: Literal["yes", "no", "choose", "go"]
    decision_id: str = ""
    ordinal: int = 0

    @classmethod
    def yes(cls, decision_id: str) -> Answer:
        return cls("yes", decision_id)

    @classmethod
    def no(cls) -> Answer:
        return cls("no")

    @classmethod
    def choose(cls, ordinal: int, decision_id: str) -> Answer:
        if ordinal < 1:
            raise ValueError("an ordinal counts from 1")
        return cls("choose", decision_id, ordinal)

    @classmethod
    def go(cls, go_id: str) -> Answer:
        return cls("go", go_id)


@dataclass(frozen=True, slots=True)
class PendingAsk:
    """What the UI shows while the runner waits."""

    kind: Literal["yes", "which", "go"]
    decision_id: str
    text: str
    risk: str
    candidates: int = 0


class AskBroker:
    def __init__(
        self,
        *,
        monitor: KillSource | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = KILL_POLL_S,
    ) -> None:
        self._monitor = monitor
        self._sleep = sleep
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._pending: PendingAsk | None = None
        self._answer: Answer | None = None

    # ---- the UI side -------------------------------------------------------------------------

    def pending(self) -> PendingAsk | None:
        with self._lock:
            return self._pending

    def answer(self, answer: Answer) -> None:
        """Post an answer for the pending ask; with none pending it is dropped."""
        with self._lock:
            if self._pending is not None:
                self._answer = answer

    # ---- the runner side ---------------------------------------------------------------------

    def confirm(self, ask: Ask) -> str:
        """The gate's confirmer call: a bound token, `no` or `killed`."""
        kind: Literal["yes", "which"] = "which" if ask.kind == "which" else "yes"
        pending = PendingAsk(
            kind=kind,
            decision_id=ask.decision_id,
            text=_ask_text(ask),
            risk=ask.risk,
            candidates=len(ask.candidates),
        )
        return self._wait(pending, lambda a: _token(a, pending))

    def go(self, go_id: str) -> str:
        """The plan preview's ask: `go`, `no` or `killed`."""
        pending = PendingAsk(kind="go", decision_id=go_id, text="", risk="plan")
        return self._wait(pending, lambda a: _go_token(a, go_id))

    def _take_answer(self) -> Answer | None:
        with self._lock:
            answer, self._answer = self._answer, None
            return answer

    def _wait(self, pending: PendingAsk, classify: Callable[[Answer], str | None]) -> str:
        with self._lock:
            if self._pending is not None:
                raise RuntimeError("one ask at a time")
            self._pending = pending
            self._answer = None  # an answer posted before this ask opened never counts
        if self._monitor is not None:
            self._monitor.mark_ask_open()
        try:
            while True:
                if self._monitor is not None and self._monitor.kill_requested():
                    return "killed"
                answer = self._take_answer()
                if answer is not None:
                    token = classify(answer)
                    if token is not None:
                        return token
                self._sleep(self._poll_s)
        finally:
            with self._lock:
                self._pending = None
                self._answer = None
            if self._monitor is not None:
                self._monitor.mark_ask_closed()


def _ask_text(ask: Ask) -> str:
    """The console's rendering without its last line (the token to type)."""
    lines = render_ask(ask).rstrip("\n").split("\n")
    return "\n".join(lines[:-1])


def _token(answer: Answer, pending: PendingAsk) -> str | None:
    if answer.kind == "no":
        return "no"
    if answer.decision_id != pending.decision_id:
        return None
    if answer.kind == "yes" and pending.kind == "yes":
        return f"yes:{pending.decision_id}"
    in_range = 1 <= answer.ordinal <= pending.candidates
    if answer.kind == "choose" and pending.kind == "which" and in_range:
        return f"{answer.ordinal}:{pending.decision_id}"
    return None


def _go_token(answer: Answer, go_id: str) -> str | None:
    if answer.kind == "no":
        return "no"
    if answer.kind == "go" and answer.decision_id == go_id:
        return "go"
    return None
