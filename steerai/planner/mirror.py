"""The pydantic mirror of the 6.3 plan file form: what the model is asked to emit.

It is pinned to the plan boundary by tests/planner/test_mirror_pins.py: every key, verb, risk,
predicate kind and decoration it allows is one `plan_io` accepts, and the step keys it leaves out
are exactly the gate's inputs (`preapproved_by_request`, `text_provenance`) and the loop's
mechanism choice (`path`). Enumerations are `Literal`s because the SDK's schema transform keeps
`enum` and drops `pattern`. Nesting is bounded by construction: `all` / `any_of` hold leaves.
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Verb = Literal["click", "focus", "type", "send", "submit", "delete"]
Risk = Literal["R0", "R1", "R2", "R3"]


class Decoration(enum.StrEnum):
    """Kept equal to `steerai.verify.decorations.DECORATIONS` by the pinning test. An Enum,
    not a Literal: a single-value Literal becomes `const`, which the SDK's schema transform
    demotes to prose, so the API would not constrain it."""

    google_search = "google_search"


MODEL_OWNED_STEP_KEYS: frozenset[str] = frozenset(
    {"id", "subgoal", "verb", "target", "text", "risk", "params", "expect"}
)


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WithinDoc(_Doc):
    landmark: str = Field(description="name of the container element (a landmark) to look in")


class AppearedDoc(_Doc):
    role: str
    name: str = Field(description="the full plain-text name of the element that appears")
    within: WithinDoc | None = None
    expected: int | None = Field(default=None, ge=1)
    order: Literal["append", "prepend"] | None = None
    decoration: Decoration | None = Field(
        default=None, description="code-owned frame around the name, e.g. google_search"
    )
    replaces: bool | None = Field(
        default=None, description="true only for a Document that replaces the previous page"
    )


class DisappearedDoc(_Doc):
    role: str
    name: str
    within: WithinDoc | None = None


class TitleDoc(_Doc):
    pattern: str = Field(description="plain text the window title must contain")


class WindowDoc(_Doc):
    name: str


class CountDeltaDoc(_Doc):
    role: str
    delta: int
    within: WithinDoc | None = None


class LeafPredicateDoc(_Doc):
    """Exactly one kind set."""

    appeared: AppearedDoc | None = None
    disappeared: DisappearedDoc | None = None
    title: TitleDoc | None = None
    window_exists: WindowDoc | None = None
    window_gone: WindowDoc | None = None
    count_delta: CountDeltaDoc | None = None


class PredicateDoc(LeafPredicateDoc):
    """Exactly one kind set; `all` / `any_of` combine leaves."""

    all: list[LeafPredicateDoc] | None = None
    any_of: list[LeafPredicateDoc] | None = None


class TargetDoc(_Doc):
    role: str
    name: str = Field(description="plain-text name as shown; for match=person the name part only")
    automation_id: str | None = None
    match: Literal["exact", "person"] | None = None


class ParamsDoc(_Doc):
    text: str | None = None
    composer_role: str | None = None
    composer_name: str | None = None
    scope: str | None = None
    content_role: str | None = None


class StepDoc(_Doc):
    id: str
    subgoal: str
    verb: Verb
    target: TargetDoc
    risk: Risk = Field(description="the tier you judge; floors and deny words can only raise it")
    text: str | None = Field(default=None, description="for type: the single-line text to type")
    params: ParamsDoc | None = None
    expect: PredicateDoc | None = Field(
        default=None, description="the step's own effect in the predicate language"
    )


class SubgoalDoc(_Doc):
    id: str
    desc: str
    condition: PredicateDoc


class PlanDoc(_Doc):
    goal: str
    subgoals: list[SubgoalDoc]
    steps: list[StepDoc]
