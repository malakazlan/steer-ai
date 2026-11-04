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

import re
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
from steerai.verify.predicates import Predicate, Within, comparable
from steerai.verify.strength import Params, strength, strengthen, template_for
from steerai.verify.t1 import redact

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
    "confirm": "R2",
    "sign out": "R2",
    "signout": "R2",
    "install": "R3",
    "uninstall": "R3",
    "format": "R3",
    "reset": "R3",
}
"""5.13 1a: words (or word sequences) in a target's name or automation id that raise the step's
tier. Matched on whole words of the comparable form (NFKC, format characters removed,
case-folded), so punctuation or a zero-width character never hides a word (RN-04)."""
_RISK_ORDER = {risk: index for index, risk in enumerate(RISKS)}
_WORD = re.compile(r"[^\W\d_]+")
"""Whole words of letters: punctuation, digits or underscores around a word ("Delete,",
"(Send)", "Delete_all", "Send2") never hide it."""
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
Outcome = Literal["approve", "confirm", "block", "pending"]


def _words(text: str) -> list[str]:
    return _WORD.findall(comparable(text))


def _id_words(automation_id: str) -> list[str]:
    # btnDelete, delete_chat, chat-delete-button: split on case changes, then on non-words
    return _words(_CAMEL.sub(" ", automation_id))


def _contains(words: list[str], pattern: str) -> bool:
    wanted = pattern.split()
    if len(wanted) == 1:
        return wanted[0] in words
    return any(words[i : i + len(wanted)] == wanted for i in range(len(words) - len(wanted) + 1))


def _declined(answer: str, risk: Risk, decision_id: str) -> GateResult:
    """Anything but a yes: a no blocks, the kill switch blocks (TK-23), silence pauses."""
    if answer in ("no", f"no:{decision_id}"):
        return _block("user_declined", risk, decision_id=decision_id)
    if answer == "killed":
        return _block("killed", risk, decision_id=decision_id)
    return GateResult(
        outcome="pending",
        decision=None,
        code=ErrorCode.GATE_PENDING_USER,
        evidence={"risk": risk, "decision_id": decision_id, "detail": "unanswered"},
    )


def effective_risk(step: Step) -> Risk:
    """The plan's tier raised by the deny patterns on the target's name and automation id,
    never lowered (RN-03/04/25)."""
    risk: Risk = step.risk
    if step.target is None:
        return risk
    words = [*_words(step.target.name), "", *_id_words(step.target.automation_id)]
    for pattern, tier in DENY_PATTERNS.items():
        if _RISK_ORDER[tier] > _RISK_ORDER[risk] and _contains(words, pattern):
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
    target_name: str = field(default="", repr=False)
    automation_id: str = field(default="", repr=False)


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


def params_for(step: Step, target_id: int | None = None, composer_id: int | None = None) -> Params:
    """Strength parameters filled by code from the step, never from model text (5.4 [v0.5]);
    `target_id` is the element the loop resolved for a role-and-name target."""
    raw = step.params
    composer = raw.get("composer_id")
    if composer is None and composer_id is not None:
        composer = str(composer_id)
    scope = raw.get("scope")
    resolved = step.target.element_id if step.target is not None else None
    return Params(
        target_id=resolved if resolved is not None else target_id,
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

    def predicate_for(
        self, step: Step, target_id: int | None = None, composer_id: int | None = None
    ) -> Predicate | None:
        """The step's effect as the gate would check it: its own `expect`, else the template."""
        if step.expect is not None:
            return step.expect
        try:
            return template_for(template_verb(step.verb), params_for(step, target_id, composer_id))
        except ValueError:
            return None

    def decide(  # noqa: PLR0913 - keyword-only context from the loop, each part optional
        self,
        step: Step,
        *,
        action_key: str,
        expect: Predicate | None,
        target_pid: int,
        previous_attempt: str | None = None,
        target_id: int | None = None,
        composer_id: int | None = None,
    ) -> GateResult:
        risk = effective_risk(step)
        if target_pid == self._own_pid:
            return _block("self_target", risk)
        params = params_for(step, target_id, composer_id)
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
            target_name=step.target.name if step.target is not None else "",
            automation_id=step.target.automation_id if step.target is not None else "",
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
        return _declined(answer, risk, decision_id)

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
