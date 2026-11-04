"""What the planner takes and returns (design 5.3 [v1.8], packet planner-2026-11-04).

The goal is the only instruction. Everything else in a request is screen content and reaches the
model as fenced data. An outcome carries the exact JSON bytes the plan was validated from, so the
runner writes and binds those bytes and never a second rendering.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import Plan


@dataclass(frozen=True, slots=True)
class PlanRequest:
    goal: str
    screen_lines: Sequence[str]
    window_title: str = ""
    app_hint: str = ""

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        object.__setattr__(self, "screen_lines", tuple(self.screen_lines))


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    plan: Plan
    data: bytes = field(repr=False)
    """The JSON bytes `plan` was validated from: what the runner writes and the ledger binds."""
    model: str
    prompt_version: str
    latency_ms: float
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def trace_record(self) -> dict[str, Any]:
        """Counts and codes only (5.14 [v1.2]): never the goal, the screen or the plan."""
        return {
            "kind": "plan",
            "model": self.model,
            "prompt_version": self.prompt_version,
            "latency_ms": self.latency_ms,
            "stop_reason": self.stop_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "steps": len(self.plan.steps),
            "subgoals": len(self.plan.subgoals),
        }


class PlannerError(SteerError):
    """The planner produced no plan; `detail` is a fixed token, `path` the field for
    `invalid_plan`. The message never quotes content."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        *,
        path: str | None = None,
        model: str = "",
        prompt_version: str = "",
    ) -> None:
        super().__init__(code, detail if path is None else f"{detail} at {path}")
        self.detail = detail
        self.path = path
        self.model = model
        self.prompt_version = prompt_version

    def trace_record(self, *, latency_ms: float) -> dict[str, Any]:
        return {
            "kind": "plan",
            "model": self.model,
            "prompt_version": self.prompt_version,
            "latency_ms": latency_ms,
            "code": self.code.value,
            "detail": self.detail,
            "path": self.path,
        }
