"""Orchestrator contracts (design 6.3 to 6.5, [v1.2]): a closed verb vocabulary, validated plans
and steps, single-use gate decisions and redacted step results.

The verb table is the only place a plan verb is mapped to an executor and to the verifier's
template; the gate refuses anything outside it. `send` and `delete` are never below R2 and R3
whatever a plan declares (5.13 deny patterns are the gate's, the floor is the contract's). Text
and names stay out of every repr because plans, decisions and results end up in traces.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from steerai.action.dispositions import ActionResult
from steerai.errors import ErrorCode
from steerai.verify.predicates import Predicate
from steerai.world.model import normalise_name

Risk = Literal["R0", "R1", "R2", "R3"]
RISKS: tuple[Risk, ...] = ("R0", "R1", "R2", "R3")
Path = Literal["semantic", "physical"]
GateOutcome = Literal["approve", "confirm", "block"]
TextProvenance = Literal["verbatim", "generated"]
MatchMode = Literal["exact", "person"]
"""How a plan's target name is matched against the screen (5.8 [v1.5]): `exact` is the whole
normalised name; `person` is the contact rule (name part equal or a whole-token prefix)."""
GateToken = GateOutcome | Literal["pending", "done_already", "unreached"]
"""`unreached`: the step failed before the gate ran; after the gate, the token is its outcome."""

VERBS: frozenset[str] = frozenset({"click", "focus", "type", "send", "submit", "delete", "open"})
_EXECUTOR_VERBS: dict[str, tuple[Path, str]] = {
    "click": ("semantic", "click"),
    "focus": ("physical", "click"),
    "type": ("physical", "type"),
    "send": ("semantic", "click"),
    "submit": ("semantic", "click"),
    "delete": ("semantic", "click"),
}
_TEXT_VERBS = frozenset({"type"})
_RISK_FLOOR: dict[str, Risk] = {"send": "R2", "submit": "R2", "delete": "R3"}
_RISK_ORDER = {risk: index for index, risk in enumerate(RISKS)}


def executor_verb(verb: str) -> tuple[Path, str]:
    """(executor path, executor verb) for a plan verb; `open` has no executor yet."""
    if verb not in VERBS:
        raise ValueError(f"unknown verb {verb!r}")
    if verb not in _EXECUTOR_VERBS:
        raise ValueError(f"{verb} has no executor in this slice (system actions are later)")
    return _EXECUTOR_VERBS[verb]


def template_verb(verb: str) -> str:
    """The verifier template's verb for a plan verb (5.10 [v0.3])."""
    if verb not in VERBS:
        raise ValueError(f"unknown verb {verb!r}")
    return verb


@dataclass(frozen=True, slots=True)
class Target:
    """What a step acts on: a role and a plain-text name (never a pattern), or for scripted plans
    a fixed element id in the step's snapshot."""

    role: str
    name: str = field(repr=False)
    automation_id: str = field(default="", repr=False)
    element_id: int | None = None
    match: MatchMode = "exact"

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise ValueError("target role must be named")
        if not normalise_name(self.name):
            raise ValueError("target name must be named")
        if self.match not in ("exact", "person"):
            raise ValueError("match must be exact or person")

    @property
    def identity(self) -> str:
        name = normalise_name(self.name).casefold()
        return f"{self.role}|{name}|{self.automation_id}|{self.match}"


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    subgoal: str
    verb: str
    target: Target | None
    text: str | None = field(default=None, repr=False)
    risk: Risk = "R1"
    params: Mapping[str, str] = field(default_factory=dict, repr=False)
    expect: Predicate | None = None
    preapproved_by_request: bool = False
    text_provenance: TextProvenance = "verbatim"
    path: Path | None = None
    """Overrides the verb table's executor path (a physical click to focus a field)."""

    def __post_init__(self) -> None:
        if self.verb not in VERBS:
            raise ValueError(f"unknown verb {self.verb!r}")
        if self.risk not in RISKS:
            raise ValueError(f"risk must be one of {RISKS}")
        if self.target is None:
            raise ValueError("a step needs a target")
        if self.verb in _TEXT_VERBS and self.text is None:
            raise ValueError(f"{self.verb} needs text")
        if self.verb not in _TEXT_VERBS and self.text is not None:
            raise ValueError(f"{self.verb} takes no text")
        composer = self.params.get("composer_id")
        if composer is not None and not composer.isdigit():
            raise ValueError("composer_id must be a snapshot element id")
        if self.path is not None and self.path not in ("semantic", "physical"):
            raise ValueError("path must be semantic or physical")
        if self.preapproved_by_request and self.text_provenance != "verbatim":
            raise ValueError("only verbatim request text can be preapproved by the request")
        floor = _RISK_FLOOR.get(self.verb)
        if floor is not None and _RISK_ORDER[self.risk] < _RISK_ORDER[floor]:
            object.__setattr__(self, "risk", floor)


@dataclass(frozen=True, slots=True)
class Subgoal:
    id: str
    desc: str = field(repr=False)
    condition: Predicate


@dataclass(frozen=True, slots=True)
class Plan:
    goal: str = field(repr=False)
    subgoals: tuple[Subgoal, ...] = ()
    steps: tuple[Step, ...] = ()

    def __post_init__(self) -> None:
        ids = {s.id for s in self.subgoals}
        if not ids:
            raise ValueError("a plan needs at least one subgoal with a condition")
        for step in self.steps:
            if step.subgoal not in ids:
                raise ValueError(f"step {step.id} names an unknown subgoal {step.subgoal}")
        step_ids = [s.id for s in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("duplicate step ids in the plan")


@dataclass(slots=True)
class GateDecision:
    """One gate outcome for one action key, consumed by exactly one execution (5.13, [v1.2])."""

    decision_id: str
    action_key: str
    outcome: GateOutcome
    consumed: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in ("approve", "confirm", "block"):
            raise ValueError("outcome must be approve, confirm or block")

    def consume(self, action_key: str) -> bool:
        if self.consumed or action_key != self.action_key or self.outcome == "block":
            return False
        self.consumed = True
        return True


@dataclass(frozen=True, slots=True)
class StepResult:
    """One step's outcome (6.5), redacted: codes, tokens, ids and latencies only."""

    step_id: str
    ok: bool
    gate: GateToken
    """`unreached`: the step failed before the gate ran (unstable, no target, no record)."""
    action: ActionResult | None
    verify_code: ErrorCode | None
    label_kind: Literal["t1", "user"] | None
    latency_ms: Mapping[str, float]
    evidence: Mapping[str, Any]
    code: ErrorCode | None = None

    def __post_init__(self) -> None:
        if self.ok and (self.verify_code is not None or self.code is not None):
            raise ValueError("an ok step carries no code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "ok": self.ok,
            "gate": self.gate,
            "action": self.action.to_dict() if self.action is not None else None,
            "verify_code": self.verify_code.value if self.verify_code else None,
            "label_kind": self.label_kind,
            "code": self.code.value if self.code else None,
            "latency_ms": dict(self.latency_ms),
            "evidence": dict(self.evidence),
        }
