"""The pydantic mirror of the 6.3 plan file form: what the model is asked to emit.

It is pinned to the plan boundary by tests/planner/test_mirror_pins.py: every key, verb, risk,
predicate kind and decoration it allows is one `plan_io` accepts, and the step keys it leaves out
are exactly the gate's inputs (`preapproved_by_request`, `text_provenance`) and the loop's
mechanism choice (`path`). Enumerations are Enums or multi-value `Literal`s because the SDK's
schema transform keeps `enum` and demotes `const` and `pattern` to prose.

The predicate is **flat**: one object with a `kind` and optional scalar fields, translated to the
file form's one-key object by `to_file_form`. The nested form (one optional object per kind, two
levels for `all` / `any_of`) compiled to a grammar the API refused as too large (first live call,
2026-11-04). `all` / `any_of` are therefore not in the model's vocabulary in this slice.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Verb = Literal["click", "focus", "type", "send", "submit", "delete", "open"]
Risk = Literal["R0", "R1", "R2", "R3"]
Kind = Literal["appeared", "disappeared", "title", "window_exists", "window_gone", "count_delta"]
ConditionKind = Literal[
    "appeared", "disappeared", "title", "window_exists", "window_gone", "count_delta", "value"
]
"""A subgoal condition's kinds: `value` is a condition only, never a step's expect, so the
grammar itself keeps it out of `expect` (field-value gate item 5)."""


class Decoration(enum.StrEnum):
    """Kept equal to `steerai.verify.decorations.DECORATIONS` by the pinning test. An Enum,
    not a Literal: a single-value Literal becomes `const`, which the SDK's schema transform
    demotes to prose, so the API would not constrain it."""

    google_search = "google_search"
    wikipedia = "wikipedia"
    youtube = "youtube"


class SiteKey(enum.StrEnum):
    """Kept equal to `steerai.action.launch.SITES` by the pinning test (an Enum: see Decoration)."""

    google_search = "google_search"
    youtube_search = "youtube_search"
    youtube = "youtube"


MODEL_OWNED_STEP_KEYS: frozenset[str] = frozenset(
    {"id", "subgoal", "verb", "target", "text", "risk", "params", "expect", "window"}
)


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PredicateDoc(_Doc):
    """One predicate: `kind` says which fields matter. appeared: role, name, within?,
    expected?, order?, decoration?, replaces?; disappeared: role, name, within?; title:
    pattern; window_exists / window_gone: name; count_delta: role, delta, within?."""

    kind: Kind
    role: str | None = None
    name: str | None = Field(default=None, description="the full plain-text name as shown")
    within: str | None = Field(default=None, description="landmark container name to look in")
    pattern: str | None = Field(default=None, description="title: plain text the title holds")
    delta: int | None = None
    expected: int | None = Field(default=None, ge=1)
    order: Literal["append", "prepend"] | None = None
    decoration: Decoration | None = Field(
        default=None, description="code-owned frame around the name, e.g. google_search"
    )
    replaces: bool | None = Field(
        default=None, description="true only for a Document that replaces the previous page"
    )

    def to_file_form(self) -> dict[str, Any]:  # Any: a JSON body for the plan boundary
        """The 6.3 one-key object; a missing required field is left out so the plan boundary
        names it (never invented here); a field the kind does not use is dropped (it never
        reaches the boundary, so it adds no matching power)."""
        fields = self.model_dump(mode="json", exclude_none=True, exclude={"kind"})
        within = fields.pop("within", None)
        wanted = _KIND_FIELDS[self.kind]
        body = {k: v for k, v in fields.items() if k in wanted}
        if within is not None and self.kind in _SCOPED_KINDS:
            body["within"] = {"landmark": within}
        return {self.kind: body}


class ConditionDoc(PredicateDoc):
    """A subgoal condition: the predicate kinds plus value: role, name, text (the field of that
    role and name holds exactly this text; only for text a type step of the subgoal typed)."""

    kind: ConditionKind  # type: ignore[assignment]  # a superset of the expect kinds, by design
    text: str | None = Field(default=None, description="value: the exact text the field holds")


_KIND_FIELDS: dict[str, frozenset[str]] = {
    "value": frozenset({"role", "name", "text"}),
    "appeared": frozenset({"role", "name", "expected", "order", "decoration", "replaces"}),
    "disappeared": frozenset({"role", "name"}),
    "count_delta": frozenset({"role", "delta"}),
    "title": frozenset({"pattern"}),
    "window_exists": frozenset({"name"}),
    "window_gone": frozenset({"name"}),
}
_SCOPED_KINDS = frozenset({"appeared", "disappeared", "count_delta"})


class TargetDoc(_Doc):
    role: str
    name: str = Field(description="plain-text name as shown; for match=person the name part only")
    automation_id: str | None = None
    match: Literal["exact", "person"] | None = None


class ParamsDoc(_Doc):
    # `site` before `text`: the model writes fields in schema order, and decided `text` before
    # the site existed (measured, see the pinning test)
    site: SiteKey | None = Field(default=None, description="for open with chrome: the site")
    text: str | None = Field(default=None, description="the content; for open: the search words")
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
    window: str | None = Field(
        default=None,
        description='"start" or "opened:<id of an earlier open step>": the window this step '
        "and the next ones act in; leave unset to stay in the window of the step before",
    )


class SubgoalDoc(_Doc):
    id: str
    desc: str
    condition: ConditionDoc


class PlanDoc(_Doc):
    goal: str
    subgoals: list[SubgoalDoc]
    steps: list[StepDoc]

    def to_file_form(self) -> dict[str, Any]:  # Any: a JSON body for the plan boundary
        """The 6.3 document with every predicate in its one-key form."""
        body = self.model_dump(mode="json", exclude_none=True)  # a model null is "not given"
        for subgoal, raw in zip(self.subgoals, body.get("subgoals", []), strict=True):
            raw["condition"] = subgoal.condition.to_file_form()
        for step, raw in zip(self.steps, body.get("steps", []), strict=True):
            if step.expect is not None:
                raw["expect"] = step.expect.to_file_form()
        return body
