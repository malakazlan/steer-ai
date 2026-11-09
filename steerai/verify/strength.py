"""Verb templates and minimum predicate strength (design 5.10 [v0.3], [v1.2]).

Each verb has a fixed template that code fills from the step's parameters (5.4 [v0.5]); the
model never writes a pattern. `Focused` alone is never a postcondition. R0/R1 accept any template
predicate; R2/R3 need a structural, content-specific `Appeared` or `Disappeared` whose pattern is
one of the step's content parameters (the text, the file name, the target's name), not the
recipient, the scope, the window title, the goal text, a timestamp or a date (VF-08, VF-39).
The weak-predicate ladder's first rung, auto-strengthening from the template, lives here; the
other rungs (replan once, ask) belong to the orchestrator.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    Focused,
    Predicate,
    State,
    StateName,
    ValueEquals,
    WindowExists,
    Within,
    presents,
)
from steerai.world.model import normalise_name

Tier = Literal["R0", "R1", "R2", "R3"]
STATE_WANTED: Mapping[str, StateName] = {"check": "on", "uncheck": "off", "select": "selected"}
"""Each state verb's wanted value: its `State` template and its own-state strength rule
(widget-verbs W2-3, W3-3)."""
MIN_PATTERN_CHARS = 3
_TIME_LIKE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?$", re.I)
_DATE_WORDS = frozenset(
    {
        "today",
        "yesterday",
        "tomorrow",
        "now",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    }
)


@dataclass(frozen=True, slots=True)
class Params:
    """The step's parameters a template may read; content values stay out of the repr."""

    target_id: int | None = None
    target_role: str | None = None
    target_name: str | None = field(default=None, repr=False)
    text: str | None = field(default=None, repr=False)
    composer_id: int | None = None
    composer_role: str = "Edit"
    composer_name: str | None = field(default=None, repr=False)
    scope: Within | None = None
    content_role: str = "Text"
    window_name: str | None = field(default=None, repr=False)
    file_name: str | None = field(default=None, repr=False)
    recipient: str | None = field(default=None, repr=False)
    window_title: str | None = field(default=None, repr=False)
    goal_text: str | None = field(default=None, repr=False)
    open_title: str | None = field(default=None, repr=False)
    """The window title an `open` must show, filled by code from the registries, never
    from a model field (schema gate P2)."""
    option: str | None = field(default=None, repr=False)
    """A choose's option (`params.option`), the ComboBox value its template reads (W4-5)."""

    def content_values(self, *, with_target: bool) -> frozenset[str]:
        """Parameters a pattern may equal: the text or file name always; the target's own
        name only for a `Disappeared` (a delete names its target; an `Appeared` equal to a
        clicked button's label proves nothing)."""
        values = [self.text, self.file_name] + ([self.target_name] if with_target else [])
        return frozenset(_key(v) for v in values if v)


@dataclass(frozen=True, slots=True)
class Strength:
    ok: bool
    reason: str | None = None


def _key(text: str) -> str:
    return normalise_name(text).casefold()


def _need_str(value: str | None, name: str, verb: str) -> str:
    if value is None:
        raise ValueError(f"{verb} template needs {name}")
    return value


def _need_int(value: int | None, name: str, verb: str) -> int:
    if value is None:
        raise ValueError(f"{verb} template needs {name}")
    return value


def _need_scope(value: Within | None, name: str, verb: str) -> Within:
    if value is None:
        raise ValueError(f"{verb} template needs {name}")
    return value


def template_for(verb: str, params: Params) -> Predicate:  # noqa: PLR0911 - one verb each
    """The verb's fixed postcondition, filled from the parameters by code."""
    if verb == "focus":
        return Focused(target_id=_need_int(params.target_id, "target_id", verb))
    if verb == "type":
        return ValueEquals(
            target_id=_need_int(params.target_id, "target_id", verb),
            text=_need_str(params.text, "text", verb),
        )
    if verb in ("send", "submit"):
        composer_id = _need_int(params.composer_id, "composer_id", verb)
        appeared = Appeared(
            role=params.content_role,
            name_pattern=_need_str(params.text, "text", verb),
            within=_need_scope(params.scope, "scope", verb),
            excluding_anchor=composer_id,
        )
        empty = ValueEquals(target_id=composer_id, text="")
        if params.composer_name is None:
            return All((appeared, empty))
        gone = Disappeared(role=params.composer_role, name_pattern=params.composer_name)
        return All((appeared, AnyOf((empty, gone))))
    if verb == "delete":
        role = _need_str(params.target_role, "target_role", verb)
        name = _need_str(params.target_name, "target_name", verb)
        return All(
            (
                Disappeared(role=role, name_pattern=name, within=params.scope),
                CountDelta(role=role, delta=-1, within=params.scope),
            )
        )
    if verb == "open":
        title = params.open_title or params.window_name
        return WindowExists(name_pattern=_need_str(title, "window_name", verb))
    if verb in STATE_WANTED:
        return State(
            target_id=_need_int(params.target_id, "target_id", verb), wanted=STATE_WANTED[verb]
        )
    if verb == "choose":
        return ValueEquals(
            target_id=_need_int(params.target_id, "target_id", verb),
            text=_need_str(params.option, "option", verb),
            folded=True,
        )
    if verb == "click":
        raise ValueError(
            "click has no template: the planner supplies the effect (appeared, disappeared, "
            "state, title or window) within the affected scope"
        )
    raise ValueError(f"unknown verb {verb!r}")


def strength(
    predicate: Predicate, tier: Tier, params: Params, *, verb: str | None = None
) -> Strength:
    if _own_state(predicate, params, verb):
        # the exact live read of the element the person approved (W2 verdict, W2-5 ruling)
        return Strength(True)
    if (
        isinstance(predicate, WindowExists)
        and params.open_title is not None
        and _key(predicate.name_pattern) == _key(params.open_title)
    ):
        # the code-owned title of a registry launch: identity-checked by the loop
        return Strength(True)
    if isinstance(predicate, Focused):
        # The honest effect of an R0 focus click; never a postcondition for anything that acts.
        return Strength(tier == "R0", None if tier == "R0" else "focused_alone")
    if presents(predicate):
        # a state proves a selection below R2, never an effect (#124, hunter PS-06)
        return Strength(tier in ("R0", "R1"), None if tier in ("R0", "R1") else "present_alone")
    if tier in ("R0", "R1"):
        return Strength(True)
    return _content_strength(predicate, params)


def _own_state(predicate: Predicate, params: Params, verb: str | None) -> bool:
    """A widget verb's own template, bound to the resolved element: top level only, never a
    click's expect, never inside all/any_of, never another verb's (W2-5 ruling). A choose's is
    its folded value of the option on the resolved ComboBox; a type's value stays weak (W4 v1
    verdict ruling (c))."""
    if verb is None or params.target_id is None:
        return False
    if verb == "choose":
        return params.option is not None and predicate == ValueEquals(
            target_id=params.target_id, text=params.option, folded=True
        )
    if verb not in STATE_WANTED:
        return False
    return predicate == State(target_id=params.target_id, wanted=STATE_WANTED[verb])


def _content_strength(predicate: Predicate, params: Params) -> Strength:
    if isinstance(predicate, AnyOf):
        branches = [_content_strength(part, params) for part in predicate.parts]
        if all(b.ok for b in branches):
            return Strength(True)
        return Strength(False, "any_of_has_a_weak_branch")
    if isinstance(predicate, All):
        parts = [_content_strength(part, params) for part in predicate.parts]
        structural = [
            s for s, part in zip(parts, predicate.parts, strict=True) if _is_structural(part)
        ]
        if any(s.ok for s in structural):
            return Strength(True)
        failing = next((s for s in structural if not s.ok), None)
        return failing or Strength(False, "no_structural_content_predicate")
    if not _is_structural(predicate):
        return Strength(False, "no_structural_content_predicate")
    assert isinstance(predicate, Appeared | Disappeared)  # noqa: S101 - narrowed above
    reason = _pattern_problem(
        predicate.name_pattern,
        predicate.within,
        params,
        with_target=isinstance(predicate, Disappeared),
    )
    return Strength(reason is None, reason)


def _is_structural(predicate: Predicate) -> bool:
    if isinstance(predicate, All | AnyOf):
        return any(_is_structural(p) for p in predicate.parts)
    return isinstance(predicate, Appeared | Disappeared)


def _pattern_problem(
    pattern: str, within: Within | None, params: Params, *, with_target: bool
) -> str | None:
    key = _key(pattern)
    scope_name = within.landmark if within is not None else None
    rules = (
        (len(key) < MIN_PATTERN_CHARS, "pattern_too_short"),
        (
            bool(params.recipient) and key == _key(params.recipient or ""),
            "pattern_is_the_recipient",
        ),
        (
            key == scope_name or (params.scope is not None and params.scope.landmark == key),
            "pattern_is_the_scope",
        ),
        (
            bool(params.window_title) and key == _key(params.window_title or ""),
            "pattern_is_the_window_title",
        ),
        (
            bool(params.goal_text) and key == _key(params.goal_text or ""),
            "pattern_is_the_goal_text",
        ),
        (_TIME_LIKE.match(key) is not None, "pattern_is_time_like"),
        (key in _DATE_WORDS, "pattern_is_date_like"),
        (key not in params.content_values(with_target=with_target), "pattern_not_a_parameter"),
    )
    return next((reason for condition, reason in rules if condition), None)


def strengthen(predicate: Predicate, verb: str, tier: Tier, params: Params) -> Predicate | None:
    """Rung 1 of the weak-predicate ladder: keep a strong predicate, else the verb template if
    that is strong, else None (the orchestrator replans once, then asks)."""
    if strength(predicate, tier, params, verb=verb).ok:
        return predicate
    try:
        candidate = template_for(verb, params)
    except ValueError:
        return None
    return candidate if strength(candidate, tier, params, verb=verb).ok else None
