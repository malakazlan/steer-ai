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
import urllib.parse
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from steerai.action.launch import SITES, LaunchError, expected_title, site_url
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import (
    RISKS,
    GateDecision,
    Risk,
    Step,
    template_verb,
)
from steerai.orchestrator.identity import Binding, CandidateLine, lines_for
from steerai.verify.predicates import Predicate, Within, comparable
from steerai.verify.strength import Params, strength, strengthen, template_for
from steerai.verify.t1 import redact
from steerai.world.elements import Element

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
_WHICH = re.compile(r"([1-9][0-9]{0,2}):(w-[0-9a-f]{12})")
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


def effective_risk(step: Step, bound_name: str | None = None) -> Risk:
    """The plan's tier raised by the deny patterns on the target's name and automation id,
    and on the bound element's name part for a person target (PT-31), never lowered."""
    risk: Risk = step.risk
    if step.target is None:
        return risk
    words = [*_words(step.target.name), "", *_id_words(step.target.automation_id)]
    if bound_name:
        words += ["", *_words(bound_name)]
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
    kind: Literal["yes", "which"] = "yes"
    """`yes`: consent to one action; `which`: the identity ask, choose among candidates."""
    candidates: tuple[CandidateLine, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class WhichResult:
    outcome: Literal["chosen", "block", "pending"]
    index: int | None
    code: ErrorCode | None
    evidence: dict[str, Any]


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


def params_for(
    step: Step,
    target_id: int | None = None,
    composer_id: int | None = None,
    bound: Binding | None = None,
) -> Params:
    """Strength parameters filled by code from the step, never from model text (5.4 [v0.5]);
    `target_id` is the element the loop resolved for a role-and-name target; for a person
    target the bound element's full name is the name the templates match (PT-09)."""
    raw = step.params
    composer = raw.get("composer_id")
    if composer is None and composer_id is not None:
        composer = str(composer_id)
    scope = raw.get("scope")
    resolved = step.target.element_id if step.target is not None else None
    return Params(
        target_id=resolved if resolved is not None else target_id,
        target_role=step.target.role if step.target is not None else None,
        target_name=(
            bound.full_name
            if bound is not None
            else step.target.name
            if step.target is not None
            else None
        ),
        text=step.text if step.text is not None else raw.get("text"),
        composer_id=int(composer) if composer is not None else None,
        composer_name=raw.get("composer_name"),
        scope=Within(landmark=scope) if scope else None,
        content_role=raw.get("content_role", "Text"),
        window_name=_open_title(step) or raw.get("window_name"),
        open_title=_open_title(step),
        file_name=raw.get("file_name"),
        recipient=raw.get("recipient"),
    )


def _open_title(step: Step) -> str | None:
    if step.verb != "open" or step.target is None:
        return None
    try:
        return expected_title(step.target.name, step.params.get("site"), step.params.get("text"))
    except LaunchError:
        return None


def open_tier(goal: str, step: Step) -> Risk:
    """An `open`'s tier from the plan's goal, by code (schema gate P1, decision 2; OW-01/02):
    content in a URL leaves the machine, so it stays R1 only when every word of it is a word
    of the goal and the goal names the site; anything else confirms at R2."""
    risk: Risk = step.risk
    site = step.params.get("site")
    text = step.params.get("text")
    if site is None or text is None:
        return risk
    goal_words = set(_words(goal))
    spec = SITES.get(site)
    named = spec is not None and bool(spec.names & goal_words)
    if named and set(_words(text)) <= goal_words:
        return risk
    return "R2" if _RISK_ORDER[risk] < _RISK_ORDER["R2"] else risk


def _opens(step: Step) -> dict[str, str]:
    """What an `open`'s ask shows besides the content: the host the URL goes to."""
    if step.verb != "open" or step.target is None:
        return {}
    try:
        url = site_url(step.target.name, step.params.get("site"), step.params.get("text"))
    except LaunchError:
        return {}
    if url is None:
        return {}
    return {"opens": urllib.parse.urlsplit(url).hostname or ""}


class Gate:
    def __init__(self, *, confirmer: Confirmer, own_pid: int, task_id: str) -> None:
        self._confirmer = confirmer
        self._own_pid = own_pid
        self._task_id = task_id

    def predicate_for(
        self,
        step: Step,
        target_id: int | None = None,
        composer_id: int | None = None,
        bound: Binding | None = None,
    ) -> Predicate | None:
        """The step's effect as the gate would check it: its own `expect`, else the template."""
        if step.expect is not None:
            return step.expect
        try:
            return template_for(
                template_verb(step.verb), params_for(step, target_id, composer_id, bound)
            )
        except ValueError:
            return None

    def which(
        self, step: Step, elements: Sequence[Element], *, previous_attempt: str | None = None
    ) -> WhichResult:
        """The identity ask (5.8 [v1.5]): several rows name the person, so the person chooses.
        Only `<n>:<ask id>` with 1 <= n <= k counts; `no` is `none_chosen`; the kill switch
        blocks; anything else pauses. No gate decision is consumed by this ask."""
        risk = effective_risk(step)
        parameter = step.target.name if step.target is not None else ""
        lines = lines_for(elements, parameter)
        ask_id = f"w-{uuid.uuid4().hex[:12]}"
        counts = {
            "candidates": len(lines),
            "lookalikes": sum(1 for line in lines if "lookalike" in line.tags),
            "mixed_script": sum(1 for line in lines if "mixed script" in line.tags),
        }
        ask = Ask(
            decision_id=ask_id,
            task_id=self._task_id,
            action_key="",
            verb=step.verb,
            target_role=step.target.role if step.target is not None else "",
            risk=risk,
            expect={},
            params={},
            previous_attempt=previous_attempt,
            target_name=parameter,
            kind="which",
            candidates=tuple(lines),
        )
        answer = self._confirmer.ask(ask)
        chosen = _WHICH.fullmatch(answer or "")
        if chosen is not None and chosen.group(2) == ask_id:
            index = int(chosen.group(1))
            if 1 <= index <= len(lines):
                return WhichResult("chosen", index, None, {**counts, "chosen_index": index})
        if answer in ("no", f"no:{ask_id}"):
            return WhichResult(
                "block", None, ErrorCode.GATE_BLOCKED, {**counts, "detail": "none_chosen"}
            )
        if answer == "killed":
            return WhichResult(
                "block", None, ErrorCode.GATE_BLOCKED, {**counts, "detail": "killed"}
            )
        return WhichResult(
            "pending", None, ErrorCode.GATE_PENDING_USER, {**counts, "detail": "unanswered"}
        )

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
        bound: Binding | None = None,
    ) -> GateResult:
        risk = effective_risk(step, bound.name_part if bound is not None else None)
        if target_pid == self._own_pid:
            return _block("self_target", risk)
        params = params_for(step, target_id, composer_id, bound)
        if step.verb == "open":
            expect = None  # the code-owned title, never the model's effect (P2)
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
            params={
                **step.params,
                **({"text": step.text} if step.text is not None else {}),
                **_opens(step),
            },
            previous_attempt=previous_attempt,
            target_name=(
                bound.full_name
                if bound is not None
                else step.target.name
                if step.target is not None
                else ""
            ),
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

    def postcondition(
        self, step: Step, expect: Predicate | None, risk: Risk, params: Params
    ) -> Predicate | GateResult:
        """The step's postcondition through the same strength rung the decision used, for a
        rebuild after the bound row's text moved on (PT-09): the template or the plan's
        expect, strengthened where the rung strengthens, blocked where it blocks."""
        return self._predicate(step, expect, risk, params)

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
