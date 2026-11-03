"""Error taxonomy (design 5.4): every failure carries exactly one code."""

import pytest

from steerai.errors import ErrorCode, SteerError


def test_codes_match_the_design_taxonomy() -> None:
    expected = {
        "perception_timeout",
        "unstable_ui",
        "element_not_found",
        "element_not_actionable",
        "focus_lost",
        "model_timeout",
        "model_error",
        "low_confidence",
        "gate_blocked",
        "gate_pending_user",
        "verify_failed",
        "verify_timeout",
        "app_crashed",
        "interrupt_modal",
        "secure_desktop",
        "window_other_desktop",
        "budget_exceeded",
        "invalid_text",
    }
    assert {code.value for code in ErrorCode} == expected


def test_steer_error_carries_code_and_message() -> None:
    err = SteerError(ErrorCode.ELEMENT_NOT_FOUND, "no element with runtime id (42, 1)")
    assert err.code is ErrorCode.ELEMENT_NOT_FOUND
    assert str(err) == "element_not_found: no element with runtime id (42, 1)"
    with pytest.raises(SteerError, match="element_not_found"):
        raise err
