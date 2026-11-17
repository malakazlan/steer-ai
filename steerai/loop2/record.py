"""The v2 action key and write-ahead record ([V2.15] Q1, Q9; V2 §8.4).

The key stays v1's scheme: verb + target identity + params, so a duplicate does not depend on
the subgoal. The subgoal id is stored in the record, to bind the proof. A v2 task never resumes:
an unsettled record refuses the start and names the action.

The functions here are pure: the durable file that holds the records is `steerai.loop2.wal`
([V2.88] L9: a v2 ledger file of its own, beside v1's and never read by it).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from steerai.loop2.claims import Phase
from steerai.loop2.decision import ActionDoc
from steerai.orchestrator.contracts import GateDecision, Risk, Target
from steerai.orchestrator.ledger import Admission, action_key
from steerai.world.effects import EffectReport
from steerai.world.elements import Element
from steerai.world.model import Snapshot

SETTLED: Final = frozenset({"delivered_verified", "done_already", "not_delivered", "no_effect"})
"""v1's settled outcomes (`ledger._DONE_OUTCOMES | _RETRY_OUTCOMES`)."""
RELEASED: Final = frozenset({"not_delivered", "no_effect"})
"""v1's outcomes that release a key (`ledger._RETRY_OUTCOMES`): nothing was sent, or `none` was
proven."""
DONE: Final = frozenset({"delivered_verified", "done_already"})
"""v1's outcomes that close a key (`ledger._DONE_OUTCOMES`)."""
OUTCOMES: Final = SETTLED | frozenset(
    {"delivery_uncertain", "delivered_unverified", "delivered", "weak_only", "unknown"}
)
"""Every outcome a record may close with: the settled ones, v1's two uncertain deliveries
(§3.4 step 11: the delivery token) and `close_token`'s unsettled ones."""
WEAK_ONLY: Final = frozenset({"focus", "own_state", "frame_changed"})
"""[V2.17] item 4: the weak signals that, alone, close a record `weak_only`."""
UNSETTLED: Final = "unsettled_action"
"""[V2.15] Q9: the start refusal's token."""
KEY_PARAMS: Final = ("text", "key", "option")
"""[V2.17] item 6: the params a v2 key covers; never the subgoal or the commit."""


@dataclass(frozen=True)
class V2Record:
    action_key: str
    subgoal: str
    decision_id: str
    verb: str
    role: str
    phase: Phase
    before: Snapshot


@dataclass(frozen=True)
class StartRefusal:
    """[V2.15] Q9: why a v2 task does not start, naming the unsettled action by closed facts."""

    token: Literal["unsettled_action"]
    verb: str
    role: str


def key_for(secret: bytes, action: ActionDoc, element: Element, *, text: str | None) -> str:
    """`ledger.action_key` over the action's verb, the element's identity (role, name,
    automation id) and the params it carries (`text`, `key`, `option`); never the subgoal or the
    commit ([V2.17] item 6). `element` is the resolved target; for a press, the element the
    press goes to (the last acted element). `text` is the resolved text, so a type by param keys
    on the span's text; with None, the action's own free text is used.

    Raises ValueError, never guesses: an unnamed target (v1's `Target` needs a name), a type
    with no text, or a resolved text other than the action's own free text."""
    if action.text is not None and text is not None and text != action.text:
        raise ValueError("the resolved text differs from the action's own text")
    resolved = text if text is not None else action.text
    if action.verb == "type" and resolved is None:
        raise ValueError("a type keys on its resolved text")
    try:
        target = Target(role=element.role, name=element.name, automation_id=element.automation_id)
    except ValueError as exc:
        raise ValueError("an action key needs a named target") from exc
    carried: dict[str, str | None] = {"text": resolved, "key": action.key, "option": action.option}
    params = {name: value for name in KEY_PARAMS if (value := carried[name]) is not None}
    return action_key(secret, action.verb, target, params)


def open_record(  # noqa: PLR0913 - the record's facts, keyword-only
    *,
    action_key: str,
    subgoal: str,
    verb: str,
    role: str,
    before: Snapshot,
    decision: GateDecision,
) -> V2Record:
    """Consume `decision` for `action_key` and return the record in phase `written`. Raises
    ValueError when the decision is for another key, already consumed, a block, or not a
    `confirm` (§3.4 step 8: bootstrap mode admits only a yes); a refused decision stays
    unconsumed."""
    if decision.outcome != "confirm":
        raise ValueError("the gate decision is not a confirm")
    if not decision.consume(action_key):
        raise ValueError("the gate decision is for another key or already consumed")
    return V2Record(
        action_key=action_key,
        subgoal=subgoal,
        decision_id=decision.decision_id,
        verb=verb,
        role=role,
        phase="written",
        before=before,
    )


def start_refusal(
    records: Sequence[V2Record], outcomes: Mapping[str, str | None]
) -> StartRefusal | None:
    """The first record whose outcome (by action key) is not settled, as a refusal; None when
    every record is settled. A key missing from `outcomes` is unsettled (unclear means fail)."""
    for record in records:
        if outcomes.get(record.action_key) not in SETTLED:
            return StartRefusal(token=UNSETTLED, verb=record.verb, role=record.role)
    return None


def close_token(report: EffectReport) -> str:
    """The outcome a dispatched R2/R3 record closes with, from its effect ([V2.17] item 4):
    `no_effect` only for a proven `none`, which releases the key as in v1; `weak_only` for
    `focus`, `own_state` or `frame_changed` alone; `unknown` for `unknown` or any unread fact;
    `delivered` for anything else. Only `RELEASED` outcomes free the key."""
    unknown_cause = any(token.startswith("unknown") for token in report.tokens)
    if "unknown" in report.classes or report.unread or unknown_cause:
        return "unknown"
    if report.classes == frozenset({"none"}) and not report.tokens:
        return "no_effect"
    seen: set[str] = set(report.classes - {"none"}) | set(report.tokens)
    if not seen:
        return "unknown"  # no class and no token: nothing proves `none`
    if seen <= WEAK_ONLY:
        return "weak_only"
    return "delivered"


def admission(risk: Risk, outcome: str | None, *, recorded: bool) -> Admission:
    """Whether an action may go: R0/R1 are always `fresh` (one dispatch per key is an R2/R3 rule;
    repeats are governed by strikes, [V2.17] item 6). At R2/R3: never recorded or a `RELEASED`
    outcome is `fresh`; `delivered_verified` or `done_already` is `done`; an open record or any
    other outcome is `confirm` (a fresh ask)."""
    if risk in {"R0", "R1"} or outcome in RELEASED:
        return "fresh"
    if outcome in DONE:
        return "done"
    if not recorded and outcome is None:
        return "fresh"
    return "confirm"


def on_kill(record: V2Record) -> str:
    """[V2.17] item 5: the outcome a record with no outcome settles with when the person's kill
    stops the task. A record in flight (`acting`) settles as `unknown`; so does a `written` one,
    since a kill never proves the dispatch was not called (only the gate's `void_token` does)."""
    del record  # every open phase settles the same: unclear means unknown
    return "unknown"


def void_token(record: V2Record) -> str:
    """The principal review of #509: input arrived after the write-ahead and before the
    dispatch, so the gate never calls the dispatch and closes the record `not_delivered` (a
    `RELEASED` outcome: the next start is not refused and a re-ask shows no previous attempt).
    Only a `written` record can be voided: `acting` is written before the dispatch is called, so
    from then on nothing proves it was not. Raises ValueError otherwise."""
    if record.phase != "written":
        raise ValueError("only a written record can be voided; the dispatch may have run")
    return "not_delivered"
