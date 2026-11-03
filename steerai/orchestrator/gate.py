"""The safety gate in bootstrap mode (design 5.13; [v0.4] bootstrap; [v1.2] bound decisions).

Until calibration exists this is the only mode: R0/R1 steps are approved with strict T1 (a
predicate that is weak after the template rung is a block, never a weaker check), R2/R3 steps
always confirm through the `Confirmer`, including text the request pre-approved. Deny patterns
on the target's name raise the tier and never lower it. A step aimed at Steer's own process is
blocked before anything else. Every approval or confirmation is a `GateDecision` bound to one
action key and consumed by one execution; an answer counts only for the open ask's decision id,
only as the exact token, so a stray Enter, a late reply or a sloppy "y" never acts. No answer
is `gate_pending_user`: nothing runs. The ask carries tokens and the parameters for the
confirmer to render; names stay out of its repr.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import (
    RISKS,
    GateDecision,
    Risk,
    Step,
    template_verb,
)
from steerai.verify.predicates import Predicate, Within
from steerai.verify.strength import Params, strength, strengthen, template_for
from steerai.verify.t1 import redact
from steerai.world.model import normalise_name

DENY_PATTERNS: Mapping[str, Risk] = {
    "delete": "R3",
    "remove": "R3",
    "pay": "R3",
    "purchase": "R3",
    "buy": "R3",
    "transfer": "R3",
    "wipe": "R3",
    "send": "R2",
    "submit": "R2",
    "post": "R2",
    "publish": "R2",
    "share": "R2",
    "reply": "R2",
    "forward": "R2",
}
"""5.13 1a: words in a target's name that raise the step's tier (whole words, case-folded)."""
_RISK_ORDER = {risk: index for index, risk in enumerate(RISKS)}
Outcome = Literal["approve", "confirm", "block", "pending"]


def effective_risk(step: Step) -> Risk:
    """The plan's tier raised by the deny patterns on the target's name, never lowered."""
    risk: Risk = step.risk
    if step.target is None:
        return risk
    words = set(normalise_name(step.target.name).casefold().split())
    for word, tier in DENY_PATTERNS.items():
        if word in words and _RISK_ORDER[tier] > _RISK_ORDER[risk]:
            risk = tier
    return risk


@dataclass(frozen=True, slots=True)
class Ask:
    """One question to the user: tokens for the trace, parameters for the confirmer to show."""

    decision_id: str
    task_id: str
    action_key: str
    verb: str
    target_role: str
    risk: Risk
    expect: Mapping[str, Any]
    params: Mapping[str, str] = field(repr=False)
    previous_attempt: str | None = None


class Confirmer(Protocol):
    """Shows the ask and returns the user's token: "yes", "no", "unanswered", or the token
    suffixed with the decision id (`yes:<id>`). Never return on a bare Enter (OR-14)."""

    def ask(self, ask: Ask) -> str: ...


@dataclass(frozen=True, slots=True)
class GateResult:
    outcome: Outcome
    decision: GateDecision | None
    code: ErrorCode | None
    evidence: dict[str, Any]
    expect: Predicate | None = None
    params: Params | None = None


def _block(detail: str, risk: Risk, **extra: Any) -> GateResult:
    return GateResult(
        outcome="block",
        decision=None,
        code=ErrorCode.GATE_BLOCKED,
        evidence={"risk": risk, "detail": detail, **extra},
    )


def params_for(step: Step) -> Params:
    """Strength parameters filled by code from the step, never from model text (5.4 [v0.5])."""
    raw = step.params
    composer = raw.get("composer_id")
    scope = raw.get("scope")
    return Params(
        target_id=step.target.element_id if step.target is not None else None,
        target_role=step.target.role if step.target is not None else None,
        target_name=step.target.name if step.target is not None else None,
        text=step.text if step.text is not None else raw.get("text"),
        composer_id=int(composer) if composer is not None else None,
        composer_name=raw.get("composer_name"),
        scope=Within(landmark=scope) if scope else None,
        content_role=raw.get("content_role", "Text"),
        window_name=raw.get("window_name"),
        file_name=raw.get("file_name"),
        recipient=raw.get("recipient"),
    )


class Gate:
    def __init__(self, *, confirmer: Confirmer, own_pid: int, task_id: str) -> None:
        self._confirmer = confirmer
        self._own_pid = own_pid
        self._task_id = task_id

    def decide(
        self,
        step: Step,
        *,
        action_key: str,
        expect: Predicate | None,
        target_pid: int,
        previous_attempt: str | None = None,
    ) -> GateResult:
        risk = effective_risk(step)
        if target_pid == self._own_pid:
            return _block("self_target", risk)
        params = params_for(step)
        predicate = self._predicate(step, expect, risk, params)
        if isinstance(predicate, GateResult):
            return predicate
        decision_id = f"d-{uuid.uuid4().hex[:12]}"
        if risk in ("R0", "R1"):
            decision = GateDecision(
                decision_id=decision_id, action_key=action_key, outcome="approve"
            )
            return GateResult(
                outcome="approve",
                decision=decision,
                code=None,
                evidence={"risk": risk},
                expect=predicate,
                params=params,
            )
        ask = Ask(
            decision_id=decision_id,
            task_id=self._task_id,
            action_key=action_key,
            verb=step.verb,
            target_role=step.target.role if step.target is not None else "",
            risk=risk,
            expect=redact(predicate),
            params={**step.params, **({"text": step.text} if step.text is not None else {})},
            previous_attempt=previous_attempt,
        )
        answer = self._confirmer.ask(ask)
        if answer in ("yes", f"yes:{decision_id}"):
            decision = GateDecision(
                decision_id=decision_id, action_key=action_key, outcome="confirm"
            )
            return GateResult(
                outcome="confirm",
                decision=decision,
                code=None,
                evidence={"risk": risk, "decision_id": decision_id},
                expect=predicate,
                params=params,
            )
        if answer in ("no", f"no:{decision_id}"):
            return _block("user_declined", risk, decision_id=decision_id)
        return GateResult(
            outcome="pending",
            decision=None,
            code=ErrorCode.GATE_PENDING_USER,
            evidence={"risk": risk, "decision_id": decision_id, "detail": "unanswered"},
        )

    def _predicate(
        self, step: Step, expect: Predicate | None, risk: Risk, params: Params
    ) -> Predicate | GateResult:
        """The step's postcondition after the template rung, or the block that stops it."""
        candidate = expect
        if candidate is None:
            try:
                candidate = template_for(template_verb(step.verb), params)
            except ValueError:
                return _block("no_predicate", risk)
        verdict = strength(candidate, risk, params)
        if verdict.ok:
            return candidate
        strengthened = strengthen(candidate, template_verb(step.verb), risk, params)
        if strengthened is None:
            return _block("weak_predicate", risk, reason=verdict.reason)
        return strengthened
