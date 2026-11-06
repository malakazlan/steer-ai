"""Delivery dispositions (design 5.9 [v0.7], [v1.0]), adopted from agent-desktop's contract.

Every action result says what is known about delivery. Only `not_delivered` is safe to retry;
anything else may have landed, so recovery must re-observe before acting again. A result is `ok`
only when delivery was verified, or when nothing was sent because the target was already in the
wanted state; an unverified or uncertain delivery always carries a code (AX-08, AX-26).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from steerai.errors import ErrorCode

Mechanism = Literal["semantic", "physical", "launch", "none"]
Outcome = Literal["succeeded", "failed", "skipped"]
ALREADY_IN_STATE = "AlreadyInState"


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
    mechanism: Mechanism
    outcome: Outcome


@dataclass(frozen=True, slots=True)
class ActionResult:
    verb: str
    delivery: Delivery
    steps: list[Step] = field(default_factory=list)
    code: ErrorCode | None = None
    evidence: dict[str, Any] = field(default_factory=dict)  # JSON-friendly, redacted upstream

    def __post_init__(self) -> None:
        unverified = self.delivery in (Delivery.DELIVERED_UNVERIFIED, Delivery.DELIVERY_UNCERTAIN)
        if unverified and self.code is None:
            raise ValueError(f"{self.delivery.value} must carry a code (verify_timeout/failed)")
        if self.delivery is Delivery.NOT_DELIVERED and self.code is None and not self.skipped:
            raise ValueError("not_delivered without a code needs an AlreadyInState step")
        if self.delivery is Delivery.DELIVERED_VERIFIED and self.code is not None:
            raise ValueError("delivered_verified cannot carry an error code")

    @property
    def skipped(self) -> bool:
        return any(s.label == ALREADY_IN_STATE and s.outcome == "skipped" for s in self.steps)

    @property
    def ok(self) -> bool:
        if self.code is not None:
            return False
        return self.delivery is Delivery.DELIVERED_VERIFIED or self.skipped

    @property
    def retry_safe(self) -> bool:
        return self.delivery.retry_safe

    @classmethod
    def already_satisfied(cls, verb: str) -> ActionResult:
        """The target is already in the wanted state: nothing sent, counts as success."""
        return cls(
            verb=verb,
            delivery=Delivery.NOT_DELIVERED,
            steps=[Step(label=ALREADY_IN_STATE, mechanism="none", outcome="skipped")],
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
