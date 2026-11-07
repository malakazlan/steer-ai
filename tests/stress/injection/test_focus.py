"""IJ-02 (#82, 6.4 / 5.9): `focus` is a physical click verified by the focus it leaves, so a
planner steered into "focus" on a Button would press it at R0. The plan contract refuses a focus
on anything but a field: the step never exists, so nothing is observed, asked or sent."""

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


def test_a_focus_on_a_button_is_refused_before_any_input() -> None:
    attack = fixture("IJ-02", "focus_button")
    assert attack.action is not None
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    with pytest.raises(ValueError, match="focus targets a field"):
        h.session.run_step(_focus("Button", attack.payload))
    assert [d for d in log if attack.action.matches(d.verb, d.role, d.name)] == []
    assert log == []
