"""[V2.67] D: the redacted per-turn trace. Each turn of a run is written as closed tokens and
numbers only, never text: the typed history lines (through the history's own grammar), the
observation facts (listed, cut, growing, young, focus state), the decision's shape (#386's
`decision_shape`), each refusal token, the watch class and its duration, the tokens used, and
every proof attempt with every condition (`ProofAttempt`, filled by the verifier).

The trace crosses the trace boundary (CLAUDE.md: names, message text and paths are redacted), so
the check is structural: `closed` walks the record and accepts only the fixed keys, numbers,
booleans, tokens from the code's own closed sets, `param:<n>` positions, and history lines the
positional grammar accepts. A history line names no model-chosen word: its subgoal ids and
param names are replaced by their positions (`positional`, the ruling's (a)), so a raw id
fails the record. A record that fails is dropped and counted by its writer, never written in
part.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Final, Literal, get_args

from steerai.action.dispositions import Delivery
from steerai.action.execution import REFUSAL_DETAILS
from steerai.loop2.controller import ErrorCode as ControllerCode
from steerai.loop2.controller import FocusState
from steerai.loop2.decision import ClaimKind, Closes, Key, Stop, ValidateToken, Verb
from steerai.loop2.history import (
    MAX_LINE,
    POSITIONAL_GRAMMAR,
    REFUSAL_TOKENS,
    ClaimResult,
    HistoryRecord,
    LogEntry,
    Proven,
    RefusalWord,
)
from steerai.loop2.loop import PORT_DETAILS, RiskWhy, TokenUsage, WatchKind
from steerai.loop2.owned import OwnedToken
from steerai.loop2.task import Commit
from steerai.loop2.verify import CheckToken, ProofToken
from steerai.orchestrator.contracts import RISKS
from steerai.world.effects import Carrier

Event = Literal["turn"]
"""The record's own event name."""
ReadKind = Literal["turn_start", "stale_reread", "after_action"]
"""[V2.67] A: which full-tier read a proof ran on."""
Tier = Literal["full", "poll"]
Skipped = Literal["poll", "unstable"]
"""Why a read was not judged ([V2.67] A: only a stable full-tier read is)."""
Gate = Literal["url", "document"]
"""The runner's own change gate, before the verifier: the address-bar URL changed, or the top
Document was replaced. None: no change, and the verifier was never called."""
ChangeBy = Literal["navigated", "url_changed_in_place"]
ActOutcome = Literal["not_sent", "skipped"]
"""An act with no delivery of its own: the runner refused it before the port
(`not_sent`), or the state was already as wanted and nothing was sent (`skipped`)."""
Unlisted = Literal["unlisted"]
"""A detail outside the shared closed sets: written as this token, never as itself."""
GuardToken = CheckToken | OwnedToken
"""A guard that rejected a row: the verifier's check tokens and the field-owned rules' own
names (`owned_by_*`, standing for [V2.67]'s rule numbers)."""


@dataclass(frozen=True)
class RefusalFact:
    refusal: RefusalWord
    detail: str | None
    """The act result's closed detail (`ACT_DETAILS`) when the refusal came from one."""


@dataclass(frozen=True)
class ActFact:
    """One act of the turn's batch, in order: its verb and its result token."""

    verb: Verb
    result: str
    """A `Delivery` value, or `ActOutcome`."""


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
    listed: int | None
    """The rows this turn's listing showed; None: unknown (no listing was reported)."""
    cut: int | None
    growing: bool
    young: bool
    focus: FocusState | None
    """The focus state the listing showed: elsewhere, unread, or None (on a listed element)."""
    decision: Mapping[str, object] | None
    """`controller.decision_shape` of the turn's decision; None when the turn made no call."""
    refusals: tuple[RefusalFact, ...]
    """Each refusal token, from `history.RefusalWord`: the producers' own closed set, with
    the act result's detail where one exists."""
    acts: tuple[ActFact, ...]
    repair: bool
    """The Decision took a repair call (more than one provider call)."""
    repair_tokens: tuple[str, ...]
    """The closed rejection tokens of each attempt that failed, in order."""
    watch: tuple[WatchFacts, ...]
    tokens: TokenUsage | None
    history: tuple[str, ...]
    """The turn's typed history lines, as `history.render` gives them for the `positional`
    record."""
    proofs: tuple[ProofAttempt, ...]


UNKNOWN: Final = "?"
"""A name not found among the task's subgoals or a subgoal's params: no grammar accepts it, so
the record fails the check and is dropped, never written with the raw name."""


def positional(
    record: HistoryRecord, subgoals: Sequence[str], known: Mapping[str, frozenset[str]]
) -> HistoryRecord:
    """[V2.67] D ruling (a): the record with every model-chosen name replaced by its position,
    the rule `decision_shape` uses: a subgoal id by its place among the task's subgoals (`s0`),
    a param name by its place among its subgoal's declared params, sorted (`param:0`). A
    subgoal id or param name is model text and may carry the request's own words."""

    def subgoal(sid: str) -> str:
        return f"s{subgoals.index(sid)}" if sid in subgoals else UNKNOWN

    if isinstance(record, LogEntry):
        param = record.param
        if param is not None:
            declared = sorted(known.get(record.subgoal or "", frozenset()))
            param = f"param:{declared.index(param)}" if param in declared else UNKNOWN
        sid = None if record.subgoal is None else subgoal(record.subgoal)
        return replace(record, subgoal=sid, param=param)
    if isinstance(record, ClaimResult | Proven):
        return replace(record, subgoal=subgoal(record.subgoal))
    return record


def turn_record(facts: TurnTrace) -> dict[str, object]:
    """The turn as a plain JSON object, before the `closed` check."""
    return {
        "event": EVENT,
        "turn": facts.turn,
        "listed": facts.listed,
        "cut": facts.cut,
        "growing": facts.growing,
        "young": facts.young,
        "focus": facts.focus,
        "decision": None if facts.decision is None else dict(facts.decision),
        "refusals": [asdict(r) for r in facts.refusals],
        "acts": [asdict(a) for a in facts.acts],
        "repair": facts.repair,
        "repair_tokens": list(facts.repair_tokens),
        "watch": [asdict(w) for w in facts.watch],
        "tokens": None if facts.tokens is None else asdict(facts.tokens),
        "history": list(facts.history),
        "proofs": [asdict(p) for p in facts.proofs],
    }


def _words(*literals: object) -> frozenset[str]:
    return frozenset(w for lit in literals for w in get_args(lit))


EVENT: Final[Event] = "turn"
CLOSED_TOKENS: Final = (
    _words(Event, Verb, Key, ClaimKind, Closes, Stop, WatchKind, FocusState, Carrier, ReadKind)
    | _words(Tier, Skipped, Gate, ChangeBy, CheckToken, OwnedToken, ProofToken)
    | REFUSAL_TOKENS
)
"""Every string a turn record may hold outside its history lines: the code's own closed sets,
never a hand list (`param_unknown` is a refusal token, `decision.ValidateToken`)."""
ACT_DETAILS: Final = REFUSAL_DETAILS | PORT_DETAILS | _words(Unlisted)
"""Every detail a turn record may carry: the executors' and the act port's own closed sets."""
ACT_RESULTS: Final = frozenset(d.value for d in Delivery) | _words(ActOutcome)
REPAIR_TOKENS: Final = _words(ValidateToken, ControllerCode)
"""What a failed attempt is rejected with: `validate`'s tokens and the controller's codes."""
KEY_TOKENS: Final[Mapping[str, frozenset[str]]] = {
    "refusal": REFUSAL_TOKENS,
    "rejections": REFUSAL_TOKENS,
    "detail": ACT_DETAILS,
    "result": ACT_RESULTS,
    "repair_tokens": REPAIR_TOKENS,
    "commit": _words(Commit),
    "risk": frozenset(RISKS),
    "why": _words(RiskWhy),
}
"""Keys whose strings must come from one closed set, not from any of them: a refusal is a
refusal token, never a verb or a carrier."""
_PARAM: Final = re.compile(r"param:[0-9]{1,3}")
KEYS: Final = frozenset(
    {
        "event", "turn", "listed", "cut", "growing", "young", "focus", "decision",
        "refusals", "watch", "tokens", "history", "proofs",
        "refusal", "detail", "acts", "result", "repair", "repair_tokens",
        # each action's declared commit, its rated tier and the rule that set it
        "commit", "risk", "why",
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


def detail_token(detail: object) -> str | None:
    """An act result's detail as the trace writes it: itself when it is in the shared closed
    sets, `unlisted` when it is any other value, None when there is none."""
    if detail is None:
        return None
    return detail if isinstance(detail, str) and detail in ACT_DETAILS else "unlisted"


def closed(record: Mapping[str, object]) -> bool:
    """The record holds no free text: fixed keys only; numbers finite; strings from the closed
    sets or `param:<n>`; history lines only under `history`, each one the positional grammar
    accepts (a raw subgoal id or param name fails it). Anything else (a name, a title, a
    path, page text) fails the whole record."""
    return _ok(record, key=None)


def _ok(value: object, *, key: str | None) -> bool:  # noqa: PLR0911 - one return per JSON kind
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        if key == "history":
            return len(value) <= MAX_LINE and POSITIONAL_GRAMMAR.fullmatch(value) is not None
        if key is not None and key in KEY_TOKENS:
            return value in KEY_TOKENS[key]
        return value in CLOSED_TOKENS or _PARAM.fullmatch(value) is not None
    if isinstance(value, list | tuple):
        return all(_ok(v, key=key) for v in value)
    if isinstance(value, Mapping):
        return all(isinstance(k, str) and k in KEYS and _ok(v, key=k) for k, v in value.items())
    return False


def closed_proofs(attempts: Sequence[ProofAttempt]) -> bool:
    """The attempts alone pass the same check (for the verifier's own tests)."""
    return closed({"proofs": [asdict(a) for a in attempts]})
