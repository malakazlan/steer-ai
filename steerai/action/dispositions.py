"""Delivery dispositions (design 5.9 [v0.7], adopted from agent-desktop's contract).

Every action result says what is known about delivery. Only `not_delivered` is safe to retry;
anything else may have landed, so recovery must re-observe before acting again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from steerai.errors import ErrorCode


class Delivery(StrEnum):
    NOT_DELIVERED = "not_delivered"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    DELIVERED_UNVERIFIED = "delivered_unverified"
    DELIVERED_VERIFIED = "delivered_verified"

    @property
    def retry_safe(self) -> bool:
        return self is Delivery.NOT_DELIVERED


@dataclass(frozen=True, slots=True)
class Step:
    label: str
    mechanism: str  # "semantic" | "physical" | "none"
    outcome: str  # "succeeded" | "failed" | "skipped"


@dataclass(frozen=True, slots=True)
class ActionResult:
    verb: str
    delivery: Delivery
    steps: list[Step] = field(default_factory=list)
    code: ErrorCode | None = None
    evidence: dict[str, Any] = field(default_factory=dict)  # JSON-friendly, redacted upstream

    @property
    def ok(self) -> bool:
        return self.code is None

    @property
    def retry_safe(self) -> bool:
        return self.delivery.retry_safe

    @classmethod
    def already_satisfied(cls, verb: str) -> ActionResult:
        """The target is already in the wanted state: nothing sent, counts as success."""
        return cls(
            verb=verb,
            delivery=Delivery.NOT_DELIVERED,
            steps=[Step(label="AlreadyInState", mechanism="none", outcome="skipped")],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verb": self.verb,
            "delivery": self.delivery.value,
            "retry_safe": self.retry_safe,
            "ok": self.ok,
            "code": self.code.value if self.code else None,
            "steps": [
                {"label": s.label, "mechanism": s.mechanism, "outcome": s.outcome}
                for s in self.steps
            ],
            "evidence": dict(self.evidence),
        }
