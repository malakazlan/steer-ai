"""A scripted `element_id` target is the plan's role too (deny-classes re-review item 2): the tier
is read from the plan's role and name, so an id pointing at another kind of element would be
judged as something it is not."""

from __future__ import annotations

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from tests.orchestrator.fakes import COMPOSER, SEND
from tests.orchestrator.test_loop import Harness


def _scripted(role: str, element_id: int) -> Step:
    return Step(
        id="x1",
        subgoal="g1",
        verb="click",
        target=Target(role=role, name="Message", element_id=element_id),
        risk="R0",
    )


def test_an_element_id_of_another_role_is_no_match() -> None:
    h = Harness()
    result = h.session.run_step(_scripted("Edit", SEND[0]))  # the Send Button, planned as an Edit
    assert (result.code, result.evidence["detail"]) == (ErrorCode.ELEMENT_NOT_FOUND, "no_match")
    assert h.semantic.calls == []


def test_an_element_id_of_the_plans_role_still_resolves() -> None:
    h = Harness()
    result = h.session.run_step(_scripted("Edit", COMPOSER[0]))
    assert result.evidence.get("detail") != "no_match", result.to_dict()
