"""The v2 action key and write-ahead record ([V2.15] Q1, Q9; V2 §8.4).

The key stays v1's scheme: verb + target identity + params, so a duplicate does not depend on
the subgoal. The subgoal id is stored in the record, to bind the proof. A v2 task never resumes:
an unsettled record refuses the start and names the action.

Interface stub for step 4; the tests in `tests/loop2/test_invariants_record.py` come first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from steerai.loop2._stub import stub
from steerai.loop2.claims import Phase
from steerai.loop2.decision import ActionDoc
from steerai.orchestrator.contracts import GateDecision, Risk
from steerai.orchestrator.ledger import Admission
from steerai.world.effects import EffectReport
from steerai.world.elements import Element
from steerai.world.model import Snapshot

SETTLED: Final = frozenset({"delivered_verified", "done_already", "not_delivered", "no_effect"})
"""v1's settled outcomes (`ledger._DONE_OUTCOMES | _RETRY_OUTCOMES`)."""
RELEASED: Final = frozenset({"not_delivered", "no_effect"})
"""v1's outcomes that release a key (`ledger._RETRY_OUTCOMES`): nothing was sent, or `none` was
proven."""


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


@stub
def key_for(secret: bytes, action: ActionDoc, element: Element, *, text: str | None) -> str:
    """`ledger.action_key` over the action's verb, the element's identity (role, name,
    automation id) and the params it carries (`text`, `key`, `option`); never the subgoal or the
    commit ([V2.17] item 6). `element` is the resolved target; for a press, the element the
    press goes to (the last acted element). `text` is the resolved text, so a type by param keys
    on the span's text."""
    raise NotImplementedError("step 4: the v2 action key")


@stub
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
    ValueError when the decision is for another key, already consumed, or a block."""
    raise NotImplementedError("step 4: the write-ahead record")


@stub
def start_refusal(
    records: Sequence[V2Record], outcomes: Mapping[str, str | None]
) -> StartRefusal | None:
    """The first record whose outcome (by action key) is not settled, as a refusal; None when
    every record is settled."""
    raise NotImplementedError("step 4: no resume")


@stub
def close_token(report: EffectReport) -> str:
    """The outcome a dispatched R2/R3 record closes with, from its effect ([V2.17] item 4):
    `no_effect` only for a proven `none`, which releases the key as in v1; `weak_only` for
    `focus`, `own_state` or `frame_changed` alone; `unknown` for `unknown` or any unread fact;
    `delivered` for anything else. Only `RELEASED` outcomes free the key."""
    raise NotImplementedError("step 4: closing a record")


@stub
def admission(risk: Risk, outcome: str | None, *, recorded: bool) -> Admission:
    """Whether an action may go: R0/R1 are always `fresh` (one dispatch per key is an R2/R3 rule;
    repeats are governed by strikes, [V2.17] item 6). At R2/R3: never recorded or a `RELEASED`
    outcome is `fresh`; `delivered_verified` or `done_already` is `done`; an open record or any
    other outcome is `confirm` (a fresh ask)."""
    raise NotImplementedError("step 4: admission")


@stub
def on_kill(record: V2Record) -> str:
    """[V2.17] item 5: the outcome a record settles with when the person's kill stops the task.
    A record in flight (`acting`) settles as `unknown`."""
    raise NotImplementedError("step 4: the kill switch")
