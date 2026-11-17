"""Error taxonomy (design 5.4): every failure carries exactly one code; recovery branches on it."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ErrorCode(StrEnum):
    PERCEPTION_TIMEOUT = "perception_timeout"
    PERCEPTION_DEGRADED = "perception_degraded"
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


DETAIL_USAGE: Final = "usage"
DETAIL_LOOP_V2_NOT_READY: Final = "loop_v2_not_ready"
DETAIL_NO_MATCH: Final = "no_match"
DETAIL_AMBIGUOUS_TARGET: Final = "ambiguous_target"
DETAIL_MINIMIZED: Final = "minimized"
DETAIL_CLOAK_UNREADABLE: Final = "cloak_unreadable"
WORLD_START_DETAILS: Final = frozenset(
    {
        "com_apartment",
        "cover_unread",
        "dpi_context",
        "foreground_refused",
        "identity_unread",
        "monitor_unavailable",
        "not_in_front",
        "nudge_refused",
        "page_hidden",
        "person_active",
        "probe_unproven_at_start",
        "restore_failed",
        "session_mutex_unavailable",
        "steer_already_running",
        "trace_needs_dpapi",
        "uia_unavailable",
        "window_changed",
        "window_cloaked",
        "window_covered",
        "window_minimized",
        "window_unreadable",
        "write_ahead_in_step_3",
        "wrong_thread",
    }
)
"""#438: the closed messages the v2 world refuses its start with (`loop2.live_world`; a test
holds every literal it raises to this set, so a new one is added here first)."""
START_DETAILS: Final = frozenset(
    {
        DETAIL_USAGE,
        DETAIL_LOOP_V2_NOT_READY,
        DETAIL_NO_MATCH,
        DETAIL_AMBIGUOUS_TARGET,
        DETAIL_MINIMIZED,
        DETAIL_CLOAK_UNREADABLE,
        *WORLD_START_DETAILS,
    }
)
"""The closed details a refused start's line carries (`cli.py`, `run.py`), as the emitters use
them; a stage report keeps these and drops any other detail (#429). A price-file or IME message
is free text and stays out."""


class SteerError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.requests_sent: int | None = None
        """Model requests sent in the decide turn that raised this (set by `DecideCalls.turn`,
        #489); None when no turn set it."""
