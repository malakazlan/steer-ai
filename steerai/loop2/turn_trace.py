"""[V2.67] D: the redacted per-turn trace. Each turn of a run is written as closed tokens and
numbers only, never text: the typed history lines (through the history's own grammar), the
observation facts (listed, cut, growing, young, focus state), the decision's shape (#386's
`decision_shape`), each refusal token, the watch class and its duration, the tokens used, and
every proof attempt with every condition (`ProofAttempt`, filled by the verifier).

The trace crosses the trace boundary (CLAUDE.md: names, message text and paths are redacted), so
the check is structural: `closed` walks the record and accepts only the fixed keys, numbers,
booleans, tokens from the code's own closed sets, `param:<n>` positions, and history lines the
history grammar accepts. A record that fails is dropped and counted by its writer, never
written in part.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Final, Literal, get_args

from steerai.loop2.controller import FocusState
from steerai.loop2.decision import ClaimKind, Closes, Key, Verb
from steerai.loop2.history import GRAMMAR, MAX_LINE, REFUSAL_TOKENS
from steerai.loop2.loop import TokenUsage, WatchKind
from steerai.loop2.owned import OwnedToken
from steerai.loop2.verify import CheckToken, ProofToken
from steerai.world.effects import Carrier

ReadKind = Literal["turn_start", "stale_reread", "after_action"]
"""[V2.67] A: which full-tier read a proof ran on."""
Tier = Literal["full", "poll"]
Skipped = Literal["poll", "unstable"]
"""Why a read was not judged ([V2.67] A: only a stable full-tier read is)."""
Gate = Literal["url", "document"]
"""The runner's own change gate, before the verifier: the address-bar URL changed, or the top
Document was replaced. None: no change, and the verifier was never called."""
ChangeBy = Literal["navigated", "url_changed_in_place"]
GuardToken = CheckToken | OwnedToken
"""A guard that rejected a row: the verifier's check tokens and the field-owned rules' own
names (`owned_by_*`, standing for [V2.67]'s rule numbers)."""


@dataclass(frozen=True)
class CarrierFact:
    carrier: Carrier
    held: bool
    """This carrier held the param on this read."""
    absent_before: bool | None
    """Absent on the subgoal's baseline; None when the baseline was unread."""


@dataclass(frozen=True)
class ParamProof:
    param: int
    """The param's position among the subgoal's declared params, sorted (`decision_shape`)."""
    carriers: tuple[CarrierFact, ...]


@dataclass(frozen=True)
class ReadFacts:
    tier: Tier
    stable: bool
    growing: bool
    young: bool


@dataclass(frozen=True)
class ProofAttempt:
    """One judgement of one subgoal on one read, with every condition, not only the verdict."""

    subgoal: int
    """The subgoal's position among the task's subgoals, never its id."""
    read_kind: ReadKind
    read: ReadFacts
    skipped: Skipped | None
    gate: Gate | None
    change: bool
    change_by: tuple[ChangeBy, ...]
    refused: tuple[CheckToken, ...]
    """What the navigation check refused with when it was no navigation at all."""
    params: tuple[ParamProof, ...]
    content: bool
    content_by: ProofToken | None
    guards: tuple[GuardToken, ...]
    proven: bool
    proven_how: ProofToken | None


@dataclass(frozen=True)
class WatchFacts:
    kind: WatchKind
    seconds: float


@dataclass(frozen=True)
class TurnTrace:
    """What one turn did, for the redacted trace: closed facts and numbers only."""

    turn: int
    listed: int
    cut: int
    growing: bool
    young: bool
    focus: FocusState | None
    """The focus state the listing showed: elsewhere, unread, or None (on a listed element)."""
    decision: Mapping[str, object] | None
    """`controller.decision_shape` of the turn's decision; None when the turn made no call."""
    refusals: tuple[str, ...]
    watch: tuple[WatchFacts, ...]
    tokens: TokenUsage | None
    history: tuple[str, ...]
    """The turn's typed history lines, as `history.render` gives them."""
    proofs: tuple[ProofAttempt, ...]


def turn_record(facts: TurnTrace) -> dict[str, object]:
    """The turn as a plain JSON object, before the `closed` check."""
    return {
        "event": "turn",
        "turn": facts.turn,
        "listed": facts.listed,
        "cut": facts.cut,
        "growing": facts.growing,
        "young": facts.young,
        "focus": facts.focus,
        "decision": None if facts.decision is None else dict(facts.decision),
        "refusals": list(facts.refusals),
        "watch": [asdict(w) for w in facts.watch],
        "tokens": None if facts.tokens is None else asdict(facts.tokens),
        "history": list(facts.history),
        "proofs": [asdict(p) for p in facts.proofs],
    }


def _words(*literals: object) -> frozenset[str]:
    return frozenset(w for lit in literals for w in get_args(lit))


CLOSED_TOKENS: Final = (
    _words(Verb, Key, ClaimKind, Closes, WatchKind, FocusState, Carrier, ReadKind, Tier)
    | _words(Skipped, Gate, ChangeBy, CheckToken, OwnedToken, ProofToken)
    | REFUSAL_TOKENS
    | {"turn", "param_unknown", "cannot", "blocked"}
)
"""Every string a turn record may hold outside its history lines: the code's own closed sets."""
_PARAM: Final = re.compile(r"param:[0-9]{1,3}")
KEYS: Final = frozenset(
    {
        "event", "turn", "listed", "cut", "growing", "young", "focus", "decision",
        "refusals", "watch", "tokens", "history", "proofs",
        # the decision's shape (`decision_shape`)
        "attempt", "rejections", "subgoals", "closes", "params", "actions", "verb",
        "in_step", "param", "text", "key", "claims", "kind", "evidence", "ask", "stop",
        # the watch and the tokens
        "seconds", "input_tokens", "output_tokens", "cache_write_5m", "cache_write_1h",
        "cache_read",
        # a proof attempt
        "subgoal", "read_kind", "read", "tier", "stable", "skipped", "gate", "change",
        "change_by", "refused", "carriers", "carrier", "held", "absent_before", "content",
        "content_by", "guards", "proven", "proven_how",
    }
)  # fmt: skip
"""Every key a turn record may hold: fixed names, never data."""


def closed(record: Mapping[str, object]) -> bool:
    """The record holds no free text: fixed keys only; numbers finite; strings from the closed
    sets or `param:<n>`; history lines only under `history`, each one the history grammar
    accepts. Anything else (a name, a title, a path, page text) fails the whole record."""
    return _ok(record, history=False)


def _ok(value: object, *, history: bool) -> bool:  # noqa: PLR0911 - one return per JSON kind
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        if history:
            return len(value) <= MAX_LINE and GRAMMAR.fullmatch(value) is not None
        return value in CLOSED_TOKENS or _PARAM.fullmatch(value) is not None
    if isinstance(value, list | tuple):
        return all(_ok(v, history=history) for v in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(k, str) and k in KEYS and _ok(v, history=k == "history")
            for k, v in value.items()
        )
    return False


def closed_proofs(attempts: Sequence[ProofAttempt]) -> bool:
    """The attempts alone pass the same check (for the verifier's own tests)."""
    return closed({"proofs": [asdict(a) for a in attempts]})
