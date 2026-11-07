"""Known gap IJ-02 (#82, 6.4 / 5.9): `focus` is a physical click verified by the focus it
leaves, with no check of the target's role, so a planner steered into "focus" on a Button
presses it at R0. A focus on anything but a field must be refused before any input."""

from __future__ import annotations

import pytest

from steerai.orchestrator.contracts import Step, Target
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_loop import Harness
from tests.stress.injection.catalogue import fixture
from tests.stress.injection.hostile import Dispatch, rebuild


def _focus(role: str, name: str) -> Step:
    return Step(id="f1", subgoal="g1", verb="focus", target=Target(role=role, name=name), risk="R0")


def test_a_focus_on_a_field_still_runs() -> None:
    """The control: focusing the composer is the verb's purpose."""
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.session.run_step(_focus("Edit", "Message"))
    assert [(d.verb, d.role) for d in log] == [("click", "Edit")]
    assert log[0].runtime_id == COMPOSER[3]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-02 #82")
def test_a_focus_on_a_button_is_refused_before_any_input() -> None:
    attack = fixture("IJ-02", "focus_button")
    assert attack.action is not None
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    result = h.session.run_step(_focus("Button", attack.payload))
    assert [d for d in log if attack.action.matches(d.verb, d.role, d.name)] == []
    assert result.ok is False
