"""Delivery dispositions (design 5.9 [v0.7]): only `not_delivered` is safe to retry."""

import json

import pytest

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
    result = ActionResult(
        verb="click", delivery=Delivery.DELIVERED_UNVERIFIED, code=ErrorCode.VERIFY_TIMEOUT
    )
    data = result.to_dict()
    assert data["delivery"] == "delivered_unverified"
    assert data["retry_safe"] is False
    assert data["code"] == "verify_timeout"
    assert data["ok"] is False


def test_to_dict_serialises_the_code_and_round_trips_through_json() -> None:
    result = ActionResult(
        verb="type",
        delivery=Delivery.NOT_DELIVERED,
        code=ErrorCode.INVALID_TEXT,
        steps=[Step(label="Sanitize", mechanism="none", outcome="failed")],
        evidence={"chars": 3},
    )
    data = result.to_dict()
    assert data["code"] == "invalid_text"
    assert data["ok"] is False
    assert data["retry_safe"] is True
    assert data["steps"] == [{"label": "Sanitize", "mechanism": "none", "outcome": "failed"}]
    assert json.loads(json.dumps(data)) == data


def test_to_dict_copies_evidence_so_callers_cannot_mutate_the_result() -> None:
    result = ActionResult(verb="click", delivery=Delivery.DELIVERED_VERIFIED, evidence={"n": 1})
    result.to_dict()["evidence"]["n"] = 2
    assert result.evidence == {"n": 1}


def test_failed_but_possibly_landed_result_is_not_retry_safe() -> None:
    result = ActionResult(
        verb="click", delivery=Delivery.DELIVERY_UNCERTAIN, code=ErrorCode.VERIFY_TIMEOUT
    )
    assert result.ok is False
    assert result.retry_safe is False


def test_already_satisfied_to_dict_has_one_skipped_step_and_no_code() -> None:
    data = ActionResult.already_satisfied("check").to_dict()
    assert data["code"] is None
    assert data["ok"] is True
    assert data["steps"] == [{"label": "AlreadyInState", "mechanism": "none", "outcome": "skipped"}]


def test_unverified_delivery_without_a_code_cannot_be_constructed() -> None:
    # Edge cases AX-08 / AX-26: an unverified delivery can never read as success.
    with pytest.raises(ValueError, match="must carry a code"):
        ActionResult(verb="click", delivery=Delivery.DELIVERED_UNVERIFIED)
    with pytest.raises(ValueError, match="AlreadyInState"):
        ActionResult(verb="click", delivery=Delivery.NOT_DELIVERED)
    with pytest.raises(ValueError, match="cannot carry"):
        ActionResult(
            verb="click", delivery=Delivery.DELIVERED_VERIFIED, code=ErrorCode.VERIFY_FAILED
        )
