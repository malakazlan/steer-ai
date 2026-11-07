"""IJ-06, pinned and not a gap (design 5.13 [v2.23]): an action on a `secure` element without
the gate's typed approval is refused by executor admission, `gate_blocked` / `secure_field`,
before any pattern call or input, on both executors."""

from __future__ import annotations

import pytest

from steerai.action.dispositions import Delivery
from steerai.action.executor import Action
from steerai.action.physical import PhysicalAction
from tests.action import test_executor as semantic
from tests.action import test_physical as physical
from tests.action.fakes import FakeHandle
from tests.stress.injection.catalogue import fixture

ATTACK = fixture("IJ-06", "secure_field")


@pytest.mark.parametrize("verb", ["set_value", "click"])
def test_the_semantic_executor_refuses_a_secure_field_without_approval(verb: str) -> None:
    handle = FakeHandle()
    snapshot = semantic._snapshot(handle, role="Edit", name="Password", secure=True)
    text = ATTACK.payload if verb == "set_value" else None
    result = semantic._executor().execute(Action(verb=verb, element_id=1, text=text), snapshot)
    assert (result.code, result.evidence.get("detail")) == (ATTACK.code, ATTACK.detail)
    assert result.delivery is Delivery.NOT_DELIVERED
    assert handle.calls == []
    assert ATTACK.payload not in str(result.to_dict())


@pytest.mark.parametrize("verb", ["type", "click"])
def test_the_physical_executor_refuses_a_secure_field_without_approval(verb: str) -> None:
    inputs = physical.FakeInput()
    snapshot = physical._snapshot(physical._edit(), role="Edit", name="Password", secure=True)
    text = ATTACK.payload if verb == "type" else None
    action = PhysicalAction(verb=verb, element_id=1, text=text)
    result = physical._executor(inputs).execute(action, snapshot)
    assert (result.code, result.evidence.get("detail")) == (ATTACK.code, ATTACK.detail)
    assert result.delivery is Delivery.NOT_DELIVERED
    assert (inputs.bursts, inputs.clicks, inputs.moves) == ([], [], [])
    assert ATTACK.payload not in str(result.to_dict())
