"""The plan file codec (design 6.3, 6.4 [v1.3]): JSON in, a validated `Plan` out, and back.

A plan file is a process boundary (CLAUDE.md): every key is known, every string is checked for
control, format, surrogate and private-use characters (they hide deny words and rewrite a
console, RN-04/05), integers are strict, the document is capped in bytes and in nesting, and
nothing snapshot-scoped (element ids, anchors) can be written in a file. Predicates are JSON
objects with exactly one kind; the live kinds (`value_equals`, `focused`, `state`) need a
snapshot id and are left to the verb templates. Errors carry the field path (`$.steps[2].risk`).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Literal, cast

from steerai.orchestrator.contracts import (
    RISKS,
    VERBS,
    Plan,
    Risk,
    Step,
    Subgoal,
    Target,
    executor_verb,
)
from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    Predicate,
    TitleMatches,
    WindowExists,
    WindowGone,
    Within,
)

MAX_PLAN_BYTES = 262_144
"""Unmeasured cap; a 20-step plan is under 8 KB (RN-33)."""
MAX_DEPTH = 4
"""Predicate nesting (`all` / `any_of`) counted from the condition itself (RN-30)."""
MAX_TEXT_CHARS = 2000
MAX_PARTS = 32
MAX_COUNT = 10_000
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PARAM_KEYS = frozenset({"text", "composer_role", "composer_name", "scope", "content_role"})
_FILE_VERBS = frozenset(v for v in VERBS if v != "open")
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
    }
)
_LIVE_KINDS = frozenset({"value_equals", "focused", "state"})
_KINDS = frozenset(
    {
        "appeared",
        "disappeared",
        "title",
        "window_exists",
        "window_gone",
        "count_delta",
        "all",
        "any_of",
    }
)


class PlanError(ValueError):
    """A plan file that cannot become a plan; `path` names the field."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


# ---- decoding --------------------------------------------------------------------------------


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
    return Within(landmark=_text(obj["landmark"], f"{path}.landmark"))


def _predicate(value: object, path: str, depth: int = 1) -> Predicate:
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
            _predicate(part, f"{inner}[{i}]", depth + 1)
            for i, part in enumerate(_list(body, inner, max_items=MAX_PARTS))
        )
        try:
            return All(parts) if kind == "all" else AnyOf(parts)
        except ValueError as exc:
            raise PlanError(inner, str(exc)) from exc
    fields = _obj(body, inner)
    if kind == "appeared":
        _keys(fields, frozenset({"role", "name", "within", "expected", "order"}), inner)
        order = fields.get("order", "append")
        if order not in ("append", "prepend"):
            raise PlanError(f"{inner}.order", "must be append or prepend")
        return Appeared(
            role=_text(fields.get("role"), f"{inner}.role"),
            name_pattern=_text(fields.get("name"), f"{inner}.name"),
            within=_within(fields.get("within"), f"{inner}.within"),
            expected=_int(fields.get("expected", 1), f"{inner}.expected", lo=1, hi=MAX_COUNT),
            order=cast("Literal['append', 'prepend']", order),
        )
    if kind == "disappeared":
        _keys(fields, frozenset({"role", "name", "within"}), inner)
        return Disappeared(
            role=_text(fields.get("role"), f"{inner}.role"),
            name_pattern=_text(fields.get("name"), f"{inner}.name"),
            within=_within(fields.get("within"), f"{inner}.within"),
        )
    if kind == "count_delta":
        _keys(fields, frozenset({"role", "delta", "within"}), inner)
        return CountDelta(
            role=_text(fields.get("role"), f"{inner}.role"),
            delta=_int(fields.get("delta"), f"{inner}.delta", lo=-MAX_COUNT, hi=MAX_COUNT),
            within=_within(fields.get("within"), f"{inner}.within"),
        )
    if kind == "title":
        _keys(fields, frozenset({"pattern"}), inner)
        return TitleMatches(pattern=_text(fields.get("pattern"), f"{inner}.pattern"))
    _keys(fields, frozenset({"name"}), inner)
    name = _text(fields.get("name"), f"{inner}.name")
    return (
        WindowExists(name_pattern=name)
        if kind == "window_exists"
        else WindowGone(name_pattern=name)
    )


def _target(value: object, path: str) -> Target:
    obj = _obj(value, path)
    _keys(obj, frozenset({"role", "name", "automation_id"}), path)
    try:
        return Target(
            role=_text(obj.get("role"), f"{path}.role", max_chars=64),
            name=_text(obj.get("name"), f"{path}.name"),
            automation_id=_text(
                obj.get("automation_id", ""), f"{path}.automation_id", max_chars=256
            ),
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
    try:
        return Step(
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
        )
    except ValueError as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError(path, str(exc)) from exc


def _subgoal(value: object, path: str) -> Subgoal:
    obj = _obj(value, path)
    _keys(obj, frozenset({"id", "desc", "condition"}), path)
    return Subgoal(
        id=_ident(obj.get("id"), f"{path}.id"),
        desc=_text(obj.get("desc", ""), f"{path}.desc"),
        condition=_predicate(obj.get("condition"), f"{path}.condition"),
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
    try:
        return Plan(goal=goal, subgoals=subgoals, steps=steps)
    except ValueError as exc:
        raise PlanError(f"{path}.steps", str(exc)) from exc


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
    return out


def _encode_within(within: Within | None) -> dict[str, Any]:
    if within is None:
        return {}
    if within.landmark is None:
        raise PlanError("$", "an anchor id is snapshot-scoped and has no file form")
    return {"within": {"landmark": within.landmark}}


def _encode(predicate: Predicate) -> dict[str, Any]:  # noqa: PLR0911 - one kind each
    if isinstance(predicate, Appeared):
        if predicate.excluding_anchor is not None:
            raise PlanError("$", "excluding_anchor is snapshot-scoped and has no file form")
        body: dict[str, Any] = {"role": predicate.role, "name": predicate.name_pattern}
        body.update(_encode_within(predicate.within))
        if predicate.expected != 1:
            body["expected"] = predicate.expected
        if predicate.order != "append":
            body["order"] = predicate.order
        return {"appeared": body}
    if isinstance(predicate, Disappeared):
        body = {"role": predicate.role, "name": predicate.name_pattern}
        body.update(_encode_within(predicate.within))
        return {"disappeared": body}
    if isinstance(predicate, CountDelta):
        body = {"role": predicate.role, "delta": predicate.delta}
        body.update(_encode_within(predicate.within))
        return {"count_delta": body}
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
