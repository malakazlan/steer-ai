"""IJ-16 pinned: a step aimed at Steer's own process (its console, where the yes is typed) is
blocked `self_target` before anything else: no ask, no record, no input. The gate checks the
snapshot's process; the physical executor re-checks the window under the point."""

from __future__ import annotations

import pytest

from steerai.action.dispositions import Delivery
from steerai.orchestrator.contracts import Step, Target
from steerai.verify.predicates import TitleMatches
from tests.action import test_physical as physical
from tests.action.fakes import FakeHandle
from tests.orchestrator.test_loop import Harness, _send
from tests.stress.injection.catalogue import fixture
from tests.stress.injection.hostile import Dispatch, HostilePage, rebuild

ATTACK = fixture("IJ-16", "own_console")
OWN_PID = 4242
"""The harness gate's own process id."""


def _console_click() -> Step:
    return Step(
        id="c1",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="Chat options"),
        risk="R0",
        expect=TitleMatches(pattern=ATTACK.payload),
    )


@pytest.mark.parametrize("step", [_send(), _console_click()], ids=["send", "click"])
def test_a_step_on_steers_own_window_is_blocked_before_the_ask(step: Step) -> None:
    h = Harness()
    h.world.pid = OWN_PID
    log: list[Dispatch] = []
    rebuild(h, log=log, observer=HostilePage(h.world, title=ATTACK.payload))
    result = h.session.run_step(step)
    assert (result.gate, result.code) == ("block", ATTACK.code)
    assert result.evidence.get("detail") == ATTACK.detail
    assert h.confirmer.asked == []
    assert log == []
    assert h.ledger.records == {}


def test_the_physical_executor_refuses_a_point_over_its_own_window() -> None:
    inputs = physical.FakeInput(own=777)
    result = physical._executor(inputs).execute(
        physical.CLICK, physical._snapshot(FakeHandle(), name=ATTACK.payload)
    )
    assert (result.code, result.evidence.get("detail")) == (ATTACK.code, ATTACK.detail)
    assert result.delivery is Delivery.NOT_DELIVERED
    assert (inputs.clicks, inputs.moves) == ([], [])
