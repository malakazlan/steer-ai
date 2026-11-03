"""Delivery dispositions (design 5.9 [v0.7]): only `not_delivered` is safe to retry."""

from steerai.action.dispositions import ActionResult, Delivery, Step
from steerai.errors import ErrorCode


def test_only_not_delivered_is_retry_safe() -> None:
    assert Delivery.NOT_DELIVERED.retry_safe is True
    for delivery in (
        Delivery.DELIVERY_UNCERTAIN,
        Delivery.DELIVERED_UNVERIFIED,
        Delivery.DELIVERED_VERIFIED,
    ):
        assert delivery.retry_safe is False


def test_result_exposes_retry_safe_and_ok() -> None:
    verified = ActionResult(
        verb="click",
        delivery=Delivery.DELIVERED_VERIFIED,
        steps=[Step(label="InvokePattern.Invoke", mechanism="semantic", outcome="succeeded")],
    )
    assert verified.ok is True
    assert verified.retry_safe is False
    refused = ActionResult(
        verb="type",
        delivery=Delivery.NOT_DELIVERED,
        code=ErrorCode.INVALID_TEXT,
        steps=[],
    )
    assert refused.ok is False
    assert refused.retry_safe is True


def test_already_satisfied_counts_as_success_with_nothing_sent() -> None:
    result = ActionResult.already_satisfied("check")
    assert result.ok is True
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.retry_safe is True
    assert result.steps[0].outcome == "skipped"


def test_result_to_dict_is_json_friendly() -> None:
    result = ActionResult(verb="click", delivery=Delivery.DELIVERED_UNVERIFIED, steps=[])
    data = result.to_dict()
    assert data["delivery"] == "delivered_unverified"
    assert data["retry_safe"] is False
    assert data["code"] is None
