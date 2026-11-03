"""Error taxonomy (design 5.4): every failure carries exactly one code; recovery branches on it."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    PERCEPTION_TIMEOUT = "perception_timeout"
    UNSTABLE_UI = "unstable_ui"
    ELEMENT_NOT_FOUND = "element_not_found"
    ELEMENT_NOT_ACTIONABLE = "element_not_actionable"
    FOCUS_LOST = "focus_lost"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_ERROR = "model_error"
    LOW_CONFIDENCE = "low_confidence"
    GATE_BLOCKED = "gate_blocked"
    GATE_PENDING_USER = "gate_pending_user"
    VERIFY_FAILED = "verify_failed"
    VERIFY_TIMEOUT = "verify_timeout"
    APP_CRASHED = "app_crashed"
    INTERRUPT_MODAL = "interrupt_modal"
    SECURE_DESKTOP = "secure_desktop"
    WINDOW_OTHER_DESKTOP = "window_other_desktop"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_TEXT = "invalid_text"


class SteerError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
