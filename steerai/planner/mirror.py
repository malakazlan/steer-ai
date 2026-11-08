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
ConditionKind = Literal[
    "appeared",
    "disappeared",
    "title",
    "window_exists",
    "window_gone",
    "count_delta",
    "value",
    "present",
]
"""One kind set for conditions and expects: a second predicate schema made the compiled grammar
too large for the API (HTTP 400, measured 2026-11-06), so `value` in an `expect` is refused by
the plan boundary at its path instead (field-value gate item 5, reality wins)."""


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


class WindowRef(enum.StrEnum):
    """Which window a step acts in (5.3 [v2.42]): an enum, so a malformed value cannot be
    written; the open step's id goes in `opened_step`. Live 2026-11-08, a free string field took
    `"start','expect_placeholder"` twice, on the first call and after the repair turn."""

    start = "start"
    opened = "opened"


MIRROR_ONLY_STEP_KEYS: frozenset[str] = frozenset({"opened_step"})
"""Mirror fields the file form never carries: `to_file_form` folds them into `window`."""

MODEL_OWNED_STEP_KEYS: frozenset[str] = frozenset(
    {"id", "subgoal", "verb", "target", "text", "risk", "params", "expect", "window"}
)


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PredicateDoc(_Doc):
    """One predicate: `kind` says which fields matter. appeared: role, name, within?,
    expected?, order?, decoration?, replaces?; disappeared: role, name, within?; title:
    pattern; window_exists / window_gone: name; count_delta: role, delta, within?; present:
    role, name, within (a selection click's expect and its one-step subgoal's condition)."""

    kind: ConditionKind
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
    text: str | None = Field(
        default=None, description="value (a subgoal condition only): the exact text the field holds"
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


_KIND_FIELDS: dict[str, frozenset[str]] = {
    "value": frozenset({"role", "name", "text"}),
    "appeared": frozenset({"role", "name", "expected", "order", "decoration", "replaces"}),
    "disappeared": frozenset({"role", "name"}),
    "count_delta": frozenset({"role", "delta"}),
    "present": frozenset({"role", "name"}),
    "title": frozenset({"pattern"}),
    "window_exists": frozenset({"name"}),
    "window_gone": frozenset({"name"}),
}
_SCOPED_KINDS = frozenset({"appeared", "disappeared", "count_delta", "present"})


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
    window: WindowRef | None = Field(
        default=None,
        description='"start" (the window the task began in) or "opened" (the window an earlier '
        "open step opened, named in opened_step): the window this step and the next ones act "
        "in; leave unset to stay in the window of the step before",
    )
    opened_step: str | None = Field(
        default=None, description='with window "opened": the id of the earlier open step'
    )


class SubgoalDoc(_Doc):
    id: str
    desc: str
    condition: PredicateDoc


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
            opened = raw.pop("opened_step", None)
            if step.window is WindowRef.start and opened is not None:
                # a contradiction: left in, so the boundary refuses the key at its path
                raw["window"] = "start"
                raw["opened_step"] = opened
            elif step.window is WindowRef.start:
                raw["window"] = "start"
            elif step.window is WindowRef.opened or opened is not None:
                # no id is left for the boundary to refuse at its path, never invented here
                raw["window"] = f"opened:{opened or ''}"
        return body
