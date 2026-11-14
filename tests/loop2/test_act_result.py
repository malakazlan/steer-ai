"""[V2.24] item 3: the act port returns what the loop needs: v1's delivery result (with its
method steps, for step 5) and the three-valued read-back, in one typed `ActResult`."""

from __future__ import annotations

import pytest

from steerai.action.dispositions import ActionResult, Delivery, Step
from steerai.errors import ErrorCode
from steerai.loop2.loop import READ_BACK_VERBS, ActResult

TYPED = Step(label="ValuePattern.SetValue", mechanism="semantic", outcome="succeeded")


def result(verb: str, delivery: Delivery, *, code: ErrorCode | None = None) -> ActionResult:
    return ActionResult(verb=verb, delivery=delivery, steps=[TYPED], code=code)


def test_the_result_carries_v1s_delivery_and_method_steps() -> None:
    done = ActResult(result("type", Delivery.DELIVERED_VERIFIED), read_back=True)
    assert done.delivery is Delivery.DELIVERED_VERIFIED
    assert done.action.steps == [TYPED], "the method stays recoverable for step 5"


def test_the_read_back_verbs_are_type_choose_and_the_state_verbs() -> None:
    assert (
        frozenset({"type", "choose", "check", "uncheck", "select", "expand", "collapse"})
        == READ_BACK_VERBS
    )


@pytest.mark.parametrize("read_back", [True, False, None])
def test_a_read_back_verb_carries_any_of_the_three_values(read_back: bool | None) -> None:
    delivery = Delivery.DELIVERED_VERIFIED if read_back else Delivery.DELIVERED_UNVERIFIED
    code = None if read_back else ErrorCode.VERIFY_FAILED
    assert (
        ActResult(result("type", delivery, code=code), read_back=read_back).read_back is read_back
    )


@pytest.mark.parametrize("verb", ["click", "press", "scroll", "focus", "wait"])
def test_a_verb_without_a_read_back_never_carries_one(verb: str) -> None:
    with pytest.raises(ValueError, match="read_back"):
        ActResult(result(verb, Delivery.DELIVERED_VERIFIED), read_back=True)
    assert ActResult(result(verb, Delivery.DELIVERED_VERIFIED), read_back=None).read_back is None


def test_nothing_sent_has_no_read_back() -> None:
    sent_nothing = result("type", Delivery.NOT_DELIVERED, code=ErrorCode.ELEMENT_NOT_ACTIONABLE)
    with pytest.raises(ValueError, match="read_back"):
        ActResult(sent_nothing, read_back=False)
    assert ActResult(sent_nothing, read_back=None).read_back is None


def test_a_state_already_wanted_is_read_back_true_with_nothing_sent() -> None:
    """v1's AlreadyInState: nothing sent, the state was read and is already the wanted one."""
    already = ActResult(ActionResult.already_satisfied("check"), read_back=True)
    assert already.delivery is Delivery.NOT_DELIVERED
    assert already.read_back is True


def test_a_verified_read_back_needs_a_verified_or_already_wanted_delivery() -> None:
    uncertain = result("type", Delivery.DELIVERY_UNCERTAIN, code=ErrorCode.VERIFY_TIMEOUT)
    with pytest.raises(ValueError, match="read_back"):
        ActResult(uncertain, read_back=True)
