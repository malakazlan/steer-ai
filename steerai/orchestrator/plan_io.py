"""The plan file codec (design 6.3, 6.4 [v1.3]): JSON in, a validated `Plan` out, and back.

A plan file is a process boundary (CLAUDE.md): every key is known, every string is checked for
control, format, surrogate and private-use characters (they hide deny words and rewrite a
console, RN-04/05), integers are strict, the document is capped in bytes and in nesting, and
nothing snapshot-scoped (element ids, anchors) can be written in a file. Predicates are JSON
objects with exactly one kind; the live kinds (`value_equals`, `focused`) need a snapshot id and
are left to the verb templates. Errors carry the field path (`$.steps[2].risk`).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

from steerai.action.launch import APPS, LaunchError, site_url
from steerai.orchestrator.contracts import (
    OPENED_PREFIX,
    RISKS,
    STATE_ROLE,
    STATE_VERBS,
    VERBS,
    Plan,
    Risk,
    Step,
    Subgoal,
    Target,
    executor_verb,
    is_window_ref,
)
from steerai.verify.decorations import DECORATIONS
from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CheckState,
    CountDelta,
    Disappeared,
    FieldValue,
    Predicate,
    Present,
    TitleMatches,
    WindowExists,
    WindowGone,
    Within,
    check_states,
    field_values,
    presents,
)
from steerai.world.model import normalise_name
from steerai.world.people import name_part

MAX_PLAN_BYTES = 262_144
"""Unmeasured cap; a 20-step plan is under 8 KB (RN-33)."""
MAX_DEPTH = 4
"""Predicate nesting (`all` / `any_of`) counted from the condition itself (RN-30)."""
MAX_TEXT_CHARS = 2000
MAX_PARTS = 32
MAX_COUNT = 10_000
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PARAM_KEYS = frozenset({"text", "composer_role", "composer_name", "scope", "content_role", "site"})
_FILE_VERBS = frozenset(VERBS)
_STEP_KEYS = frozenset(
    {
        "id",
        "subgoal",
        "verb",
        "target",
        "text",
        "risk",
        "params",
        "expect",
        "preapproved_by_request",
        "text_provenance",
        "path",
        "window",
    }
)
_LIVE_KINDS = frozenset({"value_equals", "focused"})
_KINDS = frozenset(
    {
        "appeared",
        "disappeared",
        "title",
        "window_exists",
        "window_gone",
        "count_delta",
        "value",
        "state",
        "present",
        "all",
        "any_of",
    }
)


TARGET_KEYS = frozenset({"role", "name", "automation_id", "match"})
VALUE_KEYS = frozenset({"role", "name", "text"})
STATE_KEYS = frozenset({"role", "name", "wanted"})
APPEARED_KEYS = frozenset({"role", "name", "within", "expected", "order", "decoration", "replaces"})
DISAPPEARED_KEYS = frozenset({"role", "name", "within"})
COUNT_DELTA_KEYS = frozenset({"role", "delta", "within"})
PRESENT_KEYS = frozenset({"role", "name", "within"})
TITLE_KEYS = frozenset({"pattern"})
WINDOW_KEYS = frozenset({"name"})
PREDICATE_KEYS: Mapping[str, frozenset[str]] = {
    "appeared": APPEARED_KEYS,
    "disappeared": DISAPPEARED_KEYS,
    "count_delta": COUNT_DELTA_KEYS,
    "title": TITLE_KEYS,
    "window_exists": WINDOW_KEYS,
    "window_gone": WINDOW_KEYS,
    "value": VALUE_KEYS,
    "state": STATE_KEYS,
    "present": PRESENT_KEYS,
}
FILE_VERBS = _FILE_VERBS
STEP_KEYS = _STEP_KEYS
PARAM_KEYS = _PARAM_KEYS
KINDS = _KINDS
"""Public names of the file form's key sets, so a producer (the planner's mirror) can be pinned
to the boundary in a test rather than drifting from it."""


class PlanError(ValueError):
    """A plan file that cannot become a plan; `path` names the field."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.reason = message


# ---- decoding --------------------------------------------------------------------------------


def check_runnable(plan: Plan) -> None:
    """Plan-time refusals for the planner's one repair turn (5.3 [v2.41]): a click has no
    template, so it needs an expect (the gate's `no_predicate`); a send's or submit's scope is
    where the content lands, never the content (stricter than the gate's `pattern_is_the_scope`,
    which a strong structural expect never reaches: failing safe costs one repair). Hand-written
    plan files are not checked here; the gate still blocks them at run time."""
    for index, step in enumerate(plan.steps):
        path = f"$.steps[{index}]"
        if step.verb == "click" and step.expect is None:
            raise PlanError(
                f"{path}.expect",
                "a click names its effect in expect: what it makes appear (the field, panel or "
                "page it opens); a best guess is fine: a wrong one fails that step's check, the "
                "next steps still run, and a replan comes only at the end of the plan",
            )
        scope, text = step.params.get("scope"), step.params.get("text")
        if (
            step.verb in ("send", "submit")
            and isinstance(scope, str)
            and isinstance(text, str)
            and normalise_name(scope).casefold() == normalise_name(text).casefold()
        ):
            raise PlanError(f"{path}.params.scope", "the scope is where the content lands, not it")


def plan_from_bytes(data: bytes) -> Plan:
    if len(data) > MAX_PLAN_BYTES:
        raise PlanError("$", f"plan is over {MAX_PLAN_BYTES} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PlanError("$", "plan must be utf-8 text") from exc
    return plan_from_json(text)


def plan_from_json(text: str) -> Plan:
    if len(text.encode("utf-8", "surrogatepass")) > MAX_PLAN_BYTES:
        raise PlanError("$", f"plan is over {MAX_PLAN_BYTES} bytes")
    try:
        raw: object = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except json.JSONDecodeError as exc:
        raise PlanError("$", f"not valid JSON: {exc.msg} at line {exc.lineno}") from exc
    except RecursionError as exc:
        raise PlanError("$", "nested too deep to decode") from exc
    except ValueError as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError("$", f"not a plan document: {exc}") from exc
    return _plan(raw, "$")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise PlanError("$", f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def _constant(name: str) -> object:
    raise PlanError("$", f"{name} is not a number")


def _obj(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanError(path, "must be an object")
    return cast("dict[str, Any]", value)


def _keys(obj: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    for key in obj:
        if key not in allowed:
            raise PlanError(f"{path}.{key}", "unknown key")


def _text(value: object, path: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise PlanError(path, "must be a string")
    if len(value) > max_chars:
        raise PlanError(path, f"longer than {max_chars} characters")
    for ch in value:
        if unicodedata.category(ch).startswith("C"):
            raise PlanError(path, f"control or format character U+{ord(ch):04X} is not allowed")
    return value


def _ident(value: object, path: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PlanError(path, "ids are 1 to 64 characters of A-Z a-z 0-9 _ . -")
    return value


def _int(value: object, path: str, *, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PlanError(path, "must be an integer")
    if not lo <= value <= hi:
        raise PlanError(path, f"must be between {lo} and {hi}")
    return value


def _bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise PlanError(path, "must be true or false")
    return value


def _list(value: object, path: str, *, max_items: int) -> list[Any]:
    if not isinstance(value, list):
        raise PlanError(path, "must be a list")
    if len(value) > max_items:
        raise PlanError(path, f"more than {max_items} items")
    return value


def _within(value: object, path: str) -> Within | None:
    if value is None:
        return None
    obj = _obj(value, path)
    _keys(obj, frozenset({"landmark"}), path)
    if "landmark" not in obj:
        raise PlanError(path, "within needs a landmark")
    return _built(path, lambda: Within(landmark=_text(obj["landmark"], f"{path}.landmark")))


def _built[T](path: str, make: Callable[[], T]) -> T:
    """A contract refusal (an empty pattern, a zero delta) is a plan error at this path."""
    try:
        return make()
    except ValueError as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError(path, str(exc)) from exc


def _predicate(value: object, path: str, depth: int = 1, *, condition: bool = False) -> Predicate:
    return _built(path, lambda: _predicate_body(value, path, depth, condition=condition))


def _field_value(fields: Mapping[str, Any], inner: str, *, condition: bool) -> FieldValue:
    if not condition:
        # presence with no before value never proves a step's effect (VF-30 B1)
        raise PlanError(inner, "value is a subgoal condition, never a step's expect")
    _keys(fields, VALUE_KEYS, inner)
    role = _text(fields.get("role"), f"{inner}.role", max_chars=64)
    name = _text(fields.get("name"), f"{inner}.name")
    text = _text(fields.get("text"), f"{inner}.text")
    try:
        return FieldValue(role=role, name_pattern=name, text=text)
    except ValueError as exc:
        raise PlanError(inner, str(exc)) from exc


def _check_state(fields: Mapping[str, Any], inner: str, *, condition: bool) -> CheckState:
    if not condition:
        raise PlanError(
            inner, "state is a subgoal condition; check and uncheck own their postcondition"
        )
    _keys(fields, STATE_KEYS, inner)
    role = _text(fields.get("role"), f"{inner}.role", max_chars=64)
    name = _text(fields.get("name"), f"{inner}.name")
    wanted = fields.get("wanted")
    if wanted not in ("on", "off"):
        raise PlanError(f"{inner}.wanted", "must be on or off")
    try:
        return CheckState(role=role, name_pattern=name, wanted=cast("Literal['on', 'off']", wanted))
    except ValueError as exc:
        raise PlanError(inner, str(exc)) from exc


def _appeared(fields: Mapping[str, Any], inner: str) -> Appeared:
    _keys(fields, APPEARED_KEYS, inner)
    replaces = _bool(fields.get("replaces", False), f"{inner}.replaces")
    order = fields.get("order", "append")
    if order not in ("append", "prepend"):
        raise PlanError(f"{inner}.order", "must be append or prepend")
    decoration = fields.get("decoration")
    if decoration is not None and (
        not isinstance(decoration, str) or decoration not in DECORATIONS
    ):
        raise PlanError(f"{inner}.decoration", "must be a known decoration")
    return Appeared(
        role=_text(fields.get("role"), f"{inner}.role"),
        name_pattern=_text(fields.get("name"), f"{inner}.name"),
        within=_within(fields.get("within"), f"{inner}.within"),
        expected=_int(fields.get("expected", 1), f"{inner}.expected", lo=1, hi=MAX_COUNT),
        order=cast("Literal['append', 'prepend']", order),
        decoration=decoration,
        replaces=replaces,
    )


def _predicate_body(value: object, path: str, depth: int, *, condition: bool) -> Predicate:
    if depth > MAX_DEPTH:
        raise PlanError(path, f"nested deeper than {MAX_DEPTH}")
    obj = _obj(value, path)
    if len(obj) != 1:
        raise PlanError(path, "a predicate is one object with exactly one kind")
    kind, body = next(iter(obj.items()))
    inner = f"{path}.{kind}"
    if kind in _LIVE_KINDS:
        raise PlanError(inner, "needs a snapshot element id; the verb template fills it")
    if kind not in _KINDS:
        raise PlanError(inner, "unknown predicate kind")
    if kind in ("all", "any_of"):
        parts = tuple(
            _predicate(part, f"{inner}[{i}]", depth + 1, condition=condition)
            for i, part in enumerate(_list(body, inner, max_items=MAX_PARTS))
        )
        try:
            return All(parts) if kind == "all" else AnyOf(parts)
        except ValueError as exc:
            raise PlanError(inner, str(exc)) from exc
    fields = _obj(body, inner)
    if kind == "appeared":
        return _appeared(fields, inner)
    if kind == "disappeared":
        _keys(fields, DISAPPEARED_KEYS, inner)
        return Disappeared(
            role=_text(fields.get("role"), f"{inner}.role"),
            name_pattern=_text(fields.get("name"), f"{inner}.name"),
            within=_within(fields.get("within"), f"{inner}.within"),
        )
    if kind in ("value", "state"):
        # condition-only kinds: refused under a step's expect at their own path
        made = _field_value if kind == "value" else _check_state
        return made(fields, inner, condition=condition)
    if kind == "present":
        return _present(fields, inner, depth)
    return _leaf(kind, fields, inner)


def _present(fields: Mapping[str, Any], inner: str, depth: int) -> Present:
    if depth > 1:
        # a branch of all/any_of would hold with no proof of its own (hunter PS-04)
        raise PlanError(inner, "present is never inside all or any_of")
    _keys(fields, PRESENT_KEYS, inner)
    within = _within(fields.get("within"), f"{inner}.within")
    if within is None:
        raise PlanError(f"{inner}.within", "present needs a landmark")
    return Present(
        role=_text(fields.get("role"), f"{inner}.role"),
        name_pattern=_text(fields.get("name"), f"{inner}.name"),
        within=within,
    )


def _leaf(kind: str, fields: Mapping[str, Any], inner: str) -> Predicate:
    """count_delta, title, window_exists and window_gone."""
    if kind == "count_delta":
        _keys(fields, COUNT_DELTA_KEYS, inner)
        return CountDelta(
            role=_text(fields.get("role"), f"{inner}.role"),
            delta=_int(fields.get("delta"), f"{inner}.delta", lo=-MAX_COUNT, hi=MAX_COUNT),
            within=_within(fields.get("within"), f"{inner}.within"),
        )
    if kind == "title":
        _keys(fields, TITLE_KEYS, inner)
        return TitleMatches(pattern=_text(fields.get("pattern"), f"{inner}.pattern"))
    _keys(fields, WINDOW_KEYS, inner)
    name = _text(fields.get("name"), f"{inner}.name")
    return (
        WindowExists(name_pattern=name)
        if kind == "window_exists"
        else WindowGone(name_pattern=name)
    )


def _target(value: object, path: str) -> Target:
    obj = _obj(value, path)
    _keys(obj, TARGET_KEYS, path)
    match = obj.get("match", "exact")
    if match not in ("exact", "person"):
        raise PlanError(f"{path}.match", "must be exact or person")
    if match == "person":
        raw_name = obj.get("name")
        if isinstance(raw_name, str) and (
            not name_part(raw_name) or name_part(raw_name) != raw_name.strip()
        ):
            raise PlanError(
                f"{path}.name", "a person is named by the name part alone: no separators"
            )
    try:
        return Target(
            role=_text(obj.get("role"), f"{path}.role", max_chars=64),
            name=_text(obj.get("name"), f"{path}.name"),
            automation_id=_text(
                obj.get("automation_id", ""), f"{path}.automation_id", max_chars=256
            ),
            match=cast("Literal['exact', 'person']", match),
        )
    except ValueError as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError(path, str(exc)) from exc


def _params(value: object, path: str) -> dict[str, str]:
    obj = _obj(value, path)
    _keys(obj, _PARAM_KEYS, path)
    return {key: _text(item, f"{path}.{key}") for key, item in obj.items()}


def _path(value: object, verb: str, path: str) -> Literal["semantic", "physical"] | None:
    if value is None:
        return None
    if verb == "open":
        raise PlanError(path, "an open step has no executor path")
    default = executor_verb(verb)[0]
    if value == default:
        return default
    if value == "physical" and verb == "click":
        return "physical"
    raise PlanError(path, f"no executor serves {verb} on path {value!r}")


def _step(value: object, path: str) -> Step:
    obj = _obj(value, path)
    _keys(obj, _STEP_KEYS, path)
    verb = obj.get("verb")
    if not isinstance(verb, str) or verb not in _FILE_VERBS:
        raise PlanError(f"{path}.verb", f"verb must be one of {sorted(_FILE_VERBS)}")
    risk = obj.get("risk", "R1")
    if risk not in RISKS:
        raise PlanError(f"{path}.risk", f"risk must be one of {RISKS}")
    provenance = obj.get("text_provenance", "verbatim")
    if provenance not in ("verbatim", "generated"):
        raise PlanError(f"{path}.text_provenance", "must be verbatim or generated")
    text = obj.get("text")
    if verb in STATE_VERBS and "expect" in obj:
        raise PlanError(f"{path}.expect", "the verb template owns the postcondition")
    try:
        step = Step(
            id=_ident(obj.get("id"), f"{path}.id"),
            subgoal=_ident(obj.get("subgoal"), f"{path}.subgoal"),
            verb=verb,
            target=_target(obj.get("target"), f"{path}.target"),
            text=None if text is None else _text(text, f"{path}.text"),
            risk=cast("Risk", risk),
            params=_params(obj.get("params", {}), f"{path}.params"),
            expect=None if "expect" not in obj else _predicate(obj["expect"], f"{path}.expect"),
            preapproved_by_request=_bool(
                obj.get("preapproved_by_request", False), f"{path}.preapproved_by_request"
            ),
            text_provenance=cast("Literal['verbatim', 'generated']", provenance),
            path=_path(obj.get("path"), verb, f"{path}.path"),
            window=_window(obj, verb, f"{path}.window"),
        )
    except ValueError as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError(path, str(exc)) from exc
    return _checked_present(_checked_open(step, path), path)


def _checked_present(step: Step, path: str) -> Step:
    """A `present` expect proves an R0/R1 click selected what it names (#124, 5.10 [v2.36])."""
    expect = step.expect
    if expect is None or not isinstance(expect, Present):
        return step
    where = f"{path}.expect"
    if step.verb != "click":
        raise PlanError(where, "present is the expect of a click only")
    if step.risk not in ("R0", "R1"):
        raise PlanError(where, "present proves a selection below R2, never an effect")
    wanted = normalise_name(expect.name_pattern).casefold()
    if step.target is None or normalise_name(step.target.name).casefold() != wanted:
        raise PlanError(where, "present names the clicked target")
    if wanted == expect.within.landmark:
        raise PlanError(where, "present never names its landmark")
    return step


def _window(obj: dict[str, Any], verb: str, path: str) -> str | None:
    """A reference, never a title; an explicit null is refused, not read as absent."""
    if "window" not in obj:
        return None
    value = obj["window"]
    if not is_window_ref(value):
        raise PlanError(path, "window must be start or opened:<step id>")
    if verb == "open":
        raise PlanError(path, "an open step takes no window: it opens one")
    return cast("str", value)


def _checked_open(step: Step, path: str) -> Step:
    """An `open` names a registry app and, for a browser, a registry site with its content."""
    if step.verb != "open" or step.target is None:
        return step
    if step.target.name not in APPS:
        raise PlanError(f"{path}.target.name", f"open names one of {sorted(APPS)}")
    try:
        site_url(step.target.name, step.params.get("site"), step.params.get("text"))
    except LaunchError as exc:
        raise PlanError(f"{path}.params", str(exc)) from exc
    return step


def _subgoal(value: object, path: str) -> Subgoal:
    obj = _obj(value, path)
    _keys(obj, frozenset({"id", "desc", "condition"}), path)
    return Subgoal(
        id=_ident(obj.get("id"), f"{path}.id"),
        desc=_text(obj.get("desc", ""), f"{path}.desc"),
        condition=_predicate(obj.get("condition"), f"{path}.condition", condition=True),
    )


def _plan(value: object, path: str) -> Plan:
    obj = _obj(value, path)
    _keys(obj, frozenset({"goal", "subgoals", "steps"}), path)
    goal = _text(obj.get("goal"), f"{path}.goal")
    subgoals = tuple(
        _subgoal(item, f"{path}.subgoals[{i}]")
        for i, item in enumerate(
            _list(obj.get("subgoals"), f"{path}.subgoals", max_items=MAX_PARTS)
        )
    )
    steps = tuple(
        _step(item, f"{path}.steps[{i}]")
        for i, item in enumerate(_list(obj.get("steps", []), f"{path}.steps", max_items=256))
    )
    _check_window_order(steps, path)
    _check_values(subgoals, steps, path)
    _check_states(subgoals, steps, path)
    _check_presents(subgoals, steps, path)
    try:
        return Plan(goal=goal, subgoals=subgoals, steps=steps)
    except ValueError as exc:
        raise PlanError(f"{path}.steps", str(exc)) from exc


_NO_VALUE_VERBS = frozenset({"send", "submit", "delete"})


def _check_values(subgoals: tuple[Subgoal, ...], steps: tuple[Step, ...], path: str) -> None:
    """A `value` subgoal with steps names a `type` of its own with the same role, name (the
    target rule) and exact text; it owns no send / submit / delete or R2/R3 step; its steps act
    in one window (FV-01/03/23/25/42). A subgoal without steps cannot tick and is allowed."""
    windows = _window_of_each(steps)
    for index, subgoal in enumerate(subgoals):
        wanted = field_values(subgoal.condition)
        own = [s for s in steps if s.subgoal == subgoal.id]
        if not wanted or not own:
            continue
        where = f"{path}.subgoals[{index}].condition"
        if any(s.verb in _NO_VALUE_VERBS or s.risk in ("R2", "R3") for s in own):
            raise PlanError(where, "a value subgoal holds no send, submit, delete or R2/R3 step")
        if len({windows[s.id] for s in own if s.verb != "open"}) > 1:
            raise PlanError(where, "a value subgoal acts in one window")
        typed = [s for s in own if s.verb == "type"]
        for value in wanted:
            if not any(_typed_into(s, value) for s in typed):
                raise PlanError(where, "value names the field and text a type step of it typed")


def _check_states(subgoals: tuple[Subgoal, ...], steps: tuple[Step, ...], path: str) -> None:
    """A `state` subgoal with steps names, for each state, a check (on) or uncheck (off) of its
    own on that CheckBox (the target rule: exact, by name alone); it owns no send / submit /
    delete; its steps act in one window (W2-4). Unlike `value`, an R2/R3 step is allowed: a
    consent box is R2 by design. A subgoal without steps cannot tick and is allowed."""
    windows = _window_of_each(steps)
    for index, subgoal in enumerate(subgoals):
        wanted = check_states(subgoal.condition)
        own = [s for s in steps if s.subgoal == subgoal.id]
        if not wanted or not own:
            continue
        where = f"{path}.subgoals[{index}].condition"
        if any(s.verb in _NO_VALUE_VERBS for s in own):
            raise PlanError(where, "a state subgoal holds no send, submit or delete step")
        if len({windows[s.id] for s in own if s.verb != "open"}) > 1:
            raise PlanError(where, "a state subgoal acts in one window")
        for state in wanted:
            if not any(_sets(s, state) for s in own):
                raise PlanError(
                    where, "state names the box and state a check or uncheck of it sets"
                )


def _sets(step: Step, state: CheckState) -> bool:
    target = step.target
    return (
        step.verb == ("check" if state.wanted == "on" else "uncheck")
        and target is not None
        and target.role == STATE_ROLE
        and target.match == "exact"
        and not target.automation_id  # resolved by the exact name only, as the state is
        and normalise_name(target.name).casefold() == normalise_name(state.name_pattern).casefold()
    )


def _check_presents(subgoals: tuple[Subgoal, ...], steps: tuple[Step, ...], path: str) -> None:
    """A `present` condition ticks only through its own click's bound proof: its subgoal is
    exactly one click whose expect is that present (hunter PS-03/05/50). A plan with a present
    names, for every irreversible step, where its content lands: that landmark is excluded from
    the present, so a message reading the name never proves the chat (#124 re-review)."""
    held = [s.condition for s in subgoals] + [s.expect for s in steps if s.expect is not None]
    if any(presents(p) for p in held):
        for index, step in enumerate(steps):
            irreversible = step.risk in ("R2", "R3") or step.verb in _NO_VALUE_VERBS
            if irreversible and not names_content_landmark(step):
                raise PlanError(
                    f"{path}.steps[{index}]",
                    "an irreversible step after a selection names where its content lands",
                )
    for index, subgoal in enumerate(subgoals):
        if not presents(subgoal.condition):
            continue
        own = [s for s in steps if s.subgoal == subgoal.id]
        if len(own) != 1 or own[0].verb != "click" or own[0].expect != subgoal.condition:
            raise PlanError(
                f"{path}.subgoals[{index}].condition",
                "a present subgoal is one click whose expect is that present",
            )


def names_content_landmark(step: Step) -> bool:
    """The step's `scope` or a landmark `within` in its expect, at any depth."""
    scope = step.params.get("scope")
    if isinstance(scope, str) and scope.strip():
        return True
    return step.expect is not None and _has_landmark(step.expect)


def _has_landmark(predicate: Predicate) -> bool:
    if isinstance(predicate, All | AnyOf):
        return any(_has_landmark(part) for part in predicate.parts)
    if isinstance(predicate, Appeared | Disappeared | CountDelta):
        return predicate.within is not None and predicate.within.landmark is not None
    return False


def _typed_into(step: Step, value: FieldValue) -> bool:
    target = step.target
    return (
        target is not None
        and target.match == "exact"
        and not target.automation_id  # resolved by the exact name only, as the value is
        and target.role == value.role
        and normalise_name(target.name).casefold() == normalise_name(value.name_pattern).casefold()
        and step.text == value.text
    )


def _window_of_each(steps: tuple[Step, ...]) -> dict[str, str]:
    """Each step's window in plan order (the loop's `_plan_refs`, with the link's start as
    "link"): a `window` changes it, an `open` changes it for the steps after."""
    current = "link"
    found: dict[str, str] = {}
    for step in steps:
        if step.window is not None:
            current = step.window
        found[step.id] = current
        if step.verb == "open":
            current = OPENED_PREFIX + step.id
    return found


def _check_window_order(steps: tuple[Step, ...], path: str) -> None:
    """A reference to a step of this plan names an `open` before it; an id outside the plan
    belongs to an earlier link of the task, which the runner and the ledger check (SW-18)."""
    ids = {s.id for s in steps}
    opened: set[str] = set()
    for index, step in enumerate(steps):
        ref = step.window
        if ref is not None and ref.startswith(OPENED_PREFIX):
            named = ref.removeprefix(OPENED_PREFIX)
            if named in ids and named not in opened:
                raise PlanError(
                    f"{path}.steps[{index}].window",
                    "window names a step that is not an earlier open",
                )
        if step.verb == "open":
            opened.add(step.id)


# ---- encoding --------------------------------------------------------------------------------


def plan_to_json(plan: Plan) -> str:
    """The file form of a plan: names and text in the clear (a file is not a trace)."""
    doc = {
        "goal": plan.goal,
        "subgoals": [
            {"id": s.id, "desc": s.desc, "condition": _encode(s.condition)} for s in plan.subgoals
        ],
        "steps": [_encode_step(step) for step in plan.steps],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _encode_step(step: Step) -> dict[str, Any]:
    if step.target is None or step.target.element_id is not None:
        raise PlanError("$.steps", f"step {step.id} carries a snapshot element id")
    if "composer_id" in step.params:
        raise PlanError("$.steps", f"step {step.id} carries a snapshot composer id")
    out: dict[str, Any] = {
        "id": step.id,
        "subgoal": step.subgoal,
        "verb": step.verb,
        "target": {"role": step.target.role, "name": step.target.name},
        "risk": step.risk,
    }
    if step.target.automation_id:
        out["target"]["automation_id"] = step.target.automation_id
    if step.target.match != "exact":
        out["target"]["match"] = step.target.match
    if step.text is not None:
        out["text"] = step.text
    if step.params:
        out["params"] = dict(step.params)
    if step.expect is not None:
        out["expect"] = _encode(step.expect)
    if step.preapproved_by_request:
        out["preapproved_by_request"] = True
    if step.text_provenance != "verbatim":
        out["text_provenance"] = step.text_provenance
    if step.path is not None:
        out["path"] = step.path
    if step.window is not None:
        out["window"] = step.window
    return out


def _encode_within(within: Within | None) -> dict[str, Any]:
    if within is None:
        return {}
    if within.landmark is None:
        raise PlanError("$", "an anchor id is snapshot-scoped and has no file form")
    return {"within": {"landmark": within.landmark}}


def _encode_appeared(predicate: Appeared) -> dict[str, Any]:
    if predicate.excluding_anchor is not None:
        raise PlanError("$", "excluding_anchor is snapshot-scoped and has no file form")
    body: dict[str, Any] = {"role": predicate.role, "name": predicate.name_pattern}
    body.update(_encode_within(predicate.within))
    if predicate.expected != 1:
        body["expected"] = predicate.expected
    if predicate.order != "append":
        body["order"] = predicate.order
    if predicate.decoration is not None:
        body["decoration"] = predicate.decoration
    if predicate.replaces:
        body["replaces"] = True
    return body


def encode_predicate(predicate: Predicate) -> dict[str, Any]:
    """The file form of one predicate (what the runner's plan preview shows)."""
    return _encode(predicate)


def _encode(predicate: Predicate) -> dict[str, Any]:  # noqa: PLR0911 - one kind each
    if isinstance(predicate, Appeared):
        return {"appeared": _encode_appeared(predicate)}
    if isinstance(predicate, Disappeared):
        body: dict[str, Any] = {"role": predicate.role, "name": predicate.name_pattern}
        body.update(_encode_within(predicate.within))
        return {"disappeared": body}
    if isinstance(predicate, CountDelta):
        body = {"role": predicate.role, "delta": predicate.delta}
        body.update(_encode_within(predicate.within))
        return {"count_delta": body}
    if isinstance(predicate, FieldValue):
        return {
            "value": {
                "role": predicate.role,
                "name": predicate.name_pattern,
                "text": predicate.text,
            }
        }
    if isinstance(predicate, CheckState):
        body = {"role": predicate.role, "name": predicate.name_pattern, "wanted": predicate.wanted}
        return {"state": body}
    if isinstance(predicate, Present):
        if predicate.target_id is not None or predicate.excluding:
            raise PlanError("$", "a bound present is snapshot-scoped and has no file form")
        body = {"role": predicate.role, "name": predicate.name_pattern}
        body.update(_encode_within(predicate.within))
        return {"present": body}
    if isinstance(predicate, TitleMatches):
        return {"title": {"pattern": predicate.pattern}}
    if isinstance(predicate, WindowExists):
        return {"window_exists": {"name": predicate.name_pattern}}
    if isinstance(predicate, WindowGone):
        return {"window_gone": {"name": predicate.name_pattern}}
    if isinstance(predicate, All):
        return {"all": [_encode(p) for p in predicate.parts]}
    if isinstance(predicate, AnyOf):
        return {"any_of": [_encode(p) for p in predicate.parts]}
    raise PlanError("$", f"{type(predicate).__name__} needs a snapshot id and has no file form")
