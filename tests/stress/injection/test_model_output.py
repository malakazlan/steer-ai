"""A5 no model widening (IJ-12): a model that writes the gate's own inputs is refused as
`malformed_output`, and a model that declares a send or a delete at R0 is floored by code; the
gate then asks at the floored tier. The fake `parse` validates the model's raw JSON against the
mirror the way the SDK's structured output does."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import Gate, effective_risk
from steerai.planner.claude_planner import ClaudePlanner
from steerai.planner.contracts import PlannerError
from steerai.planner.mirror import PlanDoc
from steerai.verify.predicates import Appeared, Disappeared, Within
from tests.orchestrator.fakes import FakeConfirmer
from tests.planner.test_claude_planner import _doc, _request, _Response
from tests.stress.injection.catalogue import Attack, fixture

_SET_BY_MODEL: dict[str, tuple[str, Any]] = {
    "preapproved": ("step", True),
    "provenance": ("step", "verbatim"),
    "extra_key": ("plan", True),
}
"""Where each refused fixture writes its key (the payload) and the value it sets."""


class RawParse:
    """The SDK's structured output in miniature: the model's raw JSON validated into the mirror;
    a document the mirror refuses raises pydantic's ValidationError from the call."""

    def __init__(self, raw: dict[str, Any]) -> None:  # Any: a JSON body from the model
        self._raw = json.dumps(raw)

    def __call__(self, **_kwargs: Any) -> _Response:  # Any: the SDK's keyword arguments
        return _Response(parsed_output=PlanDoc.model_validate_json(self._raw))


def _raw() -> dict[str, Any]:  # Any: a JSON body
    return _doc().model_dump(mode="json", exclude_none=True)


def _send_step(text: str) -> dict[str, Any]:  # Any: a JSON body
    return {
        "id": "s9",
        "subgoal": "g1",
        "verb": "send",
        "target": {"role": "Button", "name": "Send"},
        "risk": "R0",
        "params": {
            "text": text,
            "composer_role": "Edit",
            "composer_name": "Message",
            "scope": "Conversation",
        },
        "expect": {"kind": "appeared", "role": "Text", "name": text, "within": "Conversation"},
    }


def _delete_step(name: str) -> dict[str, Any]:  # Any: a JSON body
    return {
        "id": "s10",
        "subgoal": "g1",
        "verb": "delete",
        "target": {"role": "Button", "name": name},
        "risk": "R0",
        "params": {"scope": "Chats"},
        "expect": {"kind": "disappeared", "role": "ListItem", "name": "Amir", "within": "Chats"},
    }


@pytest.mark.parametrize("case", sorted(_SET_BY_MODEL))
def test_a_model_that_writes_a_gate_input_is_malformed_output(case: str) -> None:
    attack = fixture("IJ-12", case)
    where, value = _SET_BY_MODEL[case]
    raw = _raw()
    (raw["steps"][2] if where == "step" else raw)[attack.payload] = value
    with pytest.raises(PlannerError) as caught:
        ClaudePlanner(parse=RawParse(raw)).plan(_request())
    assert (caught.value.code, caught.value.detail) == (attack.code, attack.detail)
    assert caught.value.code is ErrorCode.MODEL_ERROR


def _floored(attack: Attack, step: dict[str, Any]) -> None:  # Any: a JSON body
    raw = _raw()
    raw["steps"].append(step)
    plan = ClaudePlanner(parse=RawParse(raw)).plan(_request()).plan
    injected = plan.steps[-1]
    assert injected.risk == attack.tier, "the contract's floor, not the model's R0"
    assert effective_risk(injected) == attack.tier
    assert not injected.preapproved_by_request
    confirmer = FakeConfirmer(default="no")
    result = Gate(confirmer=confirmer, own_pid=4242, task_id="t").decide(
        injected, action_key="k", expect=injected.expect, target_pid=777
    )
    assert [ask.risk for ask in confirmer.asked] == [attack.tier]
    assert (result.outcome, result.evidence["detail"]) == ("block", "user_declined")


def test_a_send_the_model_declares_r0_is_floored_and_asks() -> None:
    attack = fixture("IJ-12", "send_r0")
    _floored(attack, _send_step(attack.payload))


def test_a_delete_the_model_declares_r0_is_floored_and_asks() -> None:
    attack = fixture("IJ-12", "delete_r0")
    _floored(attack, _delete_step(attack.payload))


def test_the_floors_hold_for_a_step_built_in_code_too() -> None:
    """The floor is the contract's (`Step.__post_init__`), not only the planner's."""
    send = Step(
        id="s1",
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        risk="R0",
        expect=Appeared(role="Text", name_pattern="x", within=Within(landmark="Conversation")),
    )
    delete = Step(
        id="s2",
        subgoal="g1",
        verb="delete",
        target=Target(role="Button", name="Delete chat"),
        risk="R0",
        expect=Disappeared(role="ListItem", name_pattern="Amir"),
    )
    assert (send.risk, delete.risk) == ("R2", "R3")
