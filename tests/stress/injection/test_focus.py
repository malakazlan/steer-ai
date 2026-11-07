"""IJ-02 (#82, 6.4 / 5.9): `focus` is a physical click verified by the focus it leaves. The plan
contract refuses a focus on anything but a field, so a focus on a button never exists. A focus
on a page's Document still clicks whatever the page puts at its visible centre: the hit test
accepts any descendant of the target (FC-02), a known gap until focus gets its own hit rule."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from steerai.action.physical import PhysicalAction
from steerai.orchestrator.contracts import Step, Target, executor_verb
from tests.action.fakes import FakeHandle
from tests.action.test_physical import CHILD_ID, FakeInput, _executor, _snapshot, fast_clock
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_loop import Harness
from tests.stress.injection.catalogue import fixture
from tests.stress.injection.hostile import Dispatch, rebuild

__all__ = ["fast_clock"]  # the fixture is used by name below


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


def test_a_focus_on_a_button_is_refused_by_the_plan_contract() -> None:
    attack = fixture("IJ-02", "focus_button")
    with pytest.raises(ValueError, match="focus targets a field"):
        _focus("Button", attack.payload)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-02 #82")
def test_a_focus_on_a_page_document_never_clicks_a_link_at_its_centre(fast_clock: Any) -> None:
    attack = fixture("IJ-02", "focus_document_link")
    assert attack.action is not None
    snapshot = _snapshot(FakeHandle(), role="Document", name="Inbox")
    link = replace(snapshot.elements[2], role=attack.action.role, name=attack.payload)
    snapshot = replace(snapshot, elements=[*snapshot.elements[:2], link])
    _, verb = executor_verb("focus")  # the action the loop builds for a focus step
    inputs = FakeInput(hits=[CHILD_ID])  # the page put its link under the Document's centre
    _executor(inputs).execute(PhysicalAction(verb=verb, element_id=1), snapshot)
    assert inputs.clicks == []
