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
from steerai.orchestrator.contracts import Plan, is_window_ref

MAX_OPEN_WINDOWS = 20
MAX_PROGRESS_LINES = 40
"""Cap on the done steps and on the open subgoals a continuation lists (two plans of at most
`DEFAULT_MAX_STEPS` steps fit)."""


@dataclass(frozen=True, slots=True)
class DoneStep:
    """A step the task verified (T1) or found done: what it did, never its text, params,
    chosen row or evidence (replan item 5)."""

    verb: str
    role: str
    name: str


@dataclass(frozen=True, slots=True)
class OpenSubgoal:
    id: str
    desc: str


@dataclass(frozen=True, slots=True)
class FailedStep:
    step_id: str
    code: str
    """The taxonomy code (5.4), never a message."""


@dataclass(frozen=True, slots=True)
class PlanRequest:
    goal: str
    screen_lines: Sequence[str]
    window_title: str = ""
    app_hint: str = ""
    open_windows: Sequence[str] = ()
    """Titles of the open windows of registry apps (screen content, fenced; OW-21)."""
    continuation: int = 0
    """How many plans of this task came before: 0 for the first plan (replan [v2.1])."""
    done: Sequence[DoneStep] = ()
    remaining: Sequence[OpenSubgoal] = ()
    failure: FailedStep | None = None
    windows: Sequence[tuple[str, str]] = ()
    """The task's windows a step may name, as (reference, live title): screen content,
    fenced (step-window packet item 6)."""

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        object.__setattr__(self, "screen_lines", tuple(self.screen_lines))
        object.__setattr__(self, "open_windows", tuple(self.open_windows)[:MAX_OPEN_WINDOWS])
        object.__setattr__(self, "done", tuple(self.done)[:MAX_PROGRESS_LINES])
        object.__setattr__(self, "remaining", tuple(self.remaining)[:MAX_PROGRESS_LINES])
        object.__setattr__(self, "windows", tuple(self.windows)[:MAX_OPEN_WINDOWS])
        if any(not is_window_ref(ref) for ref, _title in self.windows):
            raise ValueError("a window is listed by its reference: start or opened:<id>")
        if self.continuation < 0:
            raise ValueError("continuation must not be negative")
        progress = bool(self.done or self.remaining or self.failure is not None)
        if self.continuation == 0 and progress:
            raise ValueError("progress belongs to a continuation")
        if self.continuation > 0 and not self.remaining:
            raise ValueError("a continuation needs the subgoals still open")


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
