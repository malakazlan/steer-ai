"""A kill while the console waits for an answer (TK-08/23): the confirmer polls the monitor
beside a reader thread and returns a distinct `killed` token that the gate maps to a block."""

from __future__ import annotations

import io
import os
import threading
import time
from dataclasses import dataclass

from steerai.errors import ErrorCode
from steerai.orchestrator.console import ConsoleConfirmer
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import Gate
from tests.orchestrator.fakes import FakeConfirmer
from tests.orchestrator.test_console import _ask


@dataclass
class KillSwitch:
    kill: bool = False
    asks: list[str] | None = None

    def kill_requested(self) -> bool:
        return self.kill

    def mark_ask_open(self) -> None:
        (self.asks if self.asks is not None else []).append("open")

    def mark_ask_closed(self) -> None:
        (self.asks if self.asks is not None else []).append("closed")


def test_a_kill_during_a_blocking_read_returns_killed_promptly() -> None:
    read_end, write_end = os.pipe()
    stdin = os.fdopen(read_end, "r", encoding="utf-8")
    switch = KillSwitch()
    confirmer = ConsoleConfirmer(stdin, io.StringIO(), interactive=False, monitor=switch)
    result: dict[str, str] = {}

    def ask() -> None:
        result["answer"] = confirmer.ask(_ask())

    thread = threading.Thread(target=ask)
    thread.start()
    time.sleep(0.2)
    assert thread.is_alive(), "waiting on a line nobody has typed"
    started = time.perf_counter()
    switch.kill = True
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert time.perf_counter() - started < 1.0
    assert result["answer"] == "killed"
    os.write(write_end, b"no\n")
    os.close(write_end)
    stdin.close()


def test_an_answer_still_arrives_through_the_reader_thread() -> None:
    switch = KillSwitch()
    confirmer = ConsoleConfirmer(
        io.StringIO("no\n"), io.StringIO(), interactive=True, monitor=switch
    )
    assert confirmer.ask(_ask()) == "no"


def test_a_kill_already_set_returns_killed_before_reading() -> None:
    stdin = io.StringIO("yes\n")
    switch = KillSwitch(kill=True)
    confirmer = ConsoleConfirmer(stdin, io.StringIO(), interactive=True, monitor=switch)
    assert confirmer.ask(_ask()) == "killed"


def test_the_gate_maps_killed_to_a_block_with_the_token() -> None:
    confirmer = FakeConfirmer(default="killed")
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    step = Step(
        id="d",
        subgoal="g1",
        verb="delete",
        target=Target(role="ListItem", name="Amir Khan"),
        risk="R3",
        params={"scope": "Chat list"},
    )
    result = gate.decide(step, action_key="k", expect=None, target_pid=2, target_id=3)
    assert result.outcome == "block"
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"
