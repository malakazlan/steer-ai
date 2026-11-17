"""[V2.65]: the shape errors are made impossible by the schema the prompt shows, not refused after.
The first turn's schema has the subgoals field and later turns' does not; an action is a union by
verb, each variant holding only its verb's fields (wait and press have no target). Later turns
share one static prefix, so caching is unaffected. `validate` stays the backstop."""

from __future__ import annotations

import json
from typing import Any, get_args

import pytest

from steerai.loop2.controller import PROMPT_VERSION, build_prompt, decide
from steerai.loop2.decision import (
    NO_COMMIT_VERBS,
    STEP,
    STEP_KINDS,
    STEP_VERBS,
    UNTARGETED,
    Decision,
    Verb,
    validate,
)
from steerai.loop2.history import Vocab

GOAL = "write hello in the notes"
TASK_FENCE = "taskfence01"
FENCE = "f" * 32


def _schema(subgoal_lines: list[str]) -> dict[str, Any]:  # Any: a JSON schema document
    later = bool(subgoal_lines)
    system, _ = build_prompt(
        GOAL,
        subgoal_lines,
        [],
        [("s1", "open")] if later else [],
        "Notes",
        [],
        task_fence=TASK_FENCE,
        screen_fence=FENCE,
        vocab=Vocab({"s1": frozenset({"text"})}, frozenset()) if later else None,
    )
    return dict(json.loads(system[1]))


FIRST = _schema([])
LATER = _schema(["s1 closes=value commit=none: write the text"])


def _variants(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:  # Any: schema parts
    items = schema["properties"]["actions"]["items"]
    out = {}
    for ref in items["oneOf"]:
        variant = schema["$defs"][ref["$ref"].rsplit("/", 1)[1]]
        out[variant["properties"]["verb"]["const"]] = variant
    return out


def _fits(variant: dict[str, Any], doc: dict[str, object]) -> bool:  # Any: a schema part
    """The schema's own shape rules for one action: no field it does not list, every field it
    requires, and one of its `oneOf` requirement sets when it has them."""
    props = set(variant["properties"])
    if not set(doc) <= props or not set(variant["required"]) <= set(doc):
        return False
    alternatives = variant.get("oneOf")
    return alternatives is None or sum(set(a["required"]) <= set(doc) for a in alternatives) == 1


def test_the_prompt_version_is_bumped() -> None:
    assert PROMPT_VERSION == "controller-14"


def test_only_the_first_turn_offers_subgoals() -> None:
    assert "subgoals" in FIRST["properties"]
    assert "subgoals" not in LATER["properties"]
    assert "SubgoalDoc" not in LATER["$defs"]


def test_later_turns_share_one_static_prefix() -> None:
    a, _ = build_prompt(GOAL, ["s1 closes=value commit=none: one"], [], [("s1", "open")], "A",
                        [], task_fence=TASK_FENCE, screen_fence=FENCE,
                        vocab=Vocab({"s1": frozenset()}, frozenset()))  # fmt: skip
    b, _ = build_prompt(GOAL, ["s2 closes=page commit=none: two"], [], [("s2", "open")], "B",
                        [], task_fence="taskfence02", screen_fence="e" * 32,
                        vocab=Vocab({"s2": frozenset()}, frozenset()))  # fmt: skip
    assert a == b
    first, _ = build_prompt(GOAL, [], [], [], "A", [], task_fence=TASK_FENCE, screen_fence=FENCE)
    assert first[0] == a[0]  # the rules are one block; only the schema block differs
    assert first[1] != a[1]


def test_an_action_is_a_union_by_verb_with_only_that_verbs_fields() -> None:
    for schema in (FIRST, LATER):
        variants = _variants(schema)
        assert set(variants) == set(STEP_VERBS[STEP])
        for verb, variant in variants.items():
            props = set(variant["properties"])
            assert variant["additionalProperties"] is False
            assert ("target" in props) is (verb not in UNTARGETED), verb
            assert ("target" in variant["required"]) is (verb not in UNTARGETED), verb
            assert ("key" in props) is (verb == "press"), verb
            assert ("option" in props) is (verb == "choose"), verb
            assert ("text" in props) is (verb == "type"), verb
            assert ("param" in props) is (verb == "type"), verb
            assert ("commit" in props) is (verb not in NO_COMMIT_VERBS), verb
            assert {"verb", "subgoal"} <= set(variant["required"])


def test_wait_and_press_cannot_carry_a_target() -> None:
    variants = _variants(LATER)
    assert not _fits(variants["wait"], {"verb": "wait", "subgoal": "s1", "target": 3})
    assert not _fits(variants["press"], {"verb": "press", "subgoal": "s1", "key": "Enter",
                                         "target": 3})  # fmt: skip
    assert _fits(variants["press"], {"verb": "press", "subgoal": "s1", "key": "Enter"})


def test_a_type_takes_text_or_a_param_never_both_nor_neither() -> None:
    typed = _variants(LATER)["type"]
    base: dict[str, object] = {"verb": "type", "subgoal": "s1", "target": 3}
    assert _fits(typed, {**base, "param": "text"})
    assert _fits(typed, {**base, "text": "hi"})
    assert not _fits(typed, {**base, "param": "text", "text": "hi"})
    assert not _fits(typed, base)


def _minimal(verb: str) -> dict[str, object]:
    doc: dict[str, object] = {"verb": verb, "subgoal": "s1"}
    if verb not in UNTARGETED:
        doc["target"] = 1
    if verb == "press":
        doc["key"] = "Enter"
    if verb == "choose":
        doc["option"] = "Red"
    if verb == "type":
        doc["param"] = "text"
    return doc


def test_every_shape_the_schema_offers_is_one_validate_accepts() -> None:
    """Both ways: each verb's minimal well-formed action fits its variant and passes `validate`;
    the parsed types are unchanged, so the refusals stay as the backstop."""
    variants = _variants(FIRST)
    for verb in get_args(Verb):
        if verb not in variants:
            continue
        doc = _minimal(verb)
        assert _fits(variants[verb], doc), verb
        decision = Decision.model_validate(
            {
                "subgoals": [
                    {
                        "id": "s1",
                        "desc": "d",
                        "params": {"text": "hello"},
                        "closes": "state",
                        "wanted": "on",
                    }
                ],
                "actions": [doc],
            }
        )
        assert validate(decision, listed_ids=frozenset({1}), goal=GOAL, first_turn=True) == []


# ---- claims: a union by kind, each holding only its kind's fields ------------------------------


def _claim_variants(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:  # Any: schema parts
    items = schema["properties"]["claims"]["items"]
    out = {}
    for ref in items["oneOf"]:
        variant = schema["$defs"][ref["$ref"].rsplit("/", 1)[1]]
        out[variant["properties"]["kind"]["const"]] = variant
    return out


def test_a_claim_is_a_union_by_kind_with_only_that_kinds_fields() -> None:
    for schema in (FIRST, LATER):
        variants = _claim_variants(schema)
        assert set(variants) == set(STEP_KINDS[STEP])
        assert "Claim" not in schema["$defs"]
        for variant in variants.values():
            assert variant["additionalProperties"] is False
            assert {"subgoal", "kind"} <= set(variant["required"])
            assert "answer" not in variant["properties"]  # the answer kind is not in step 3
        value, state, nav = variants["value"], variants["state"], variants["navigated"]
        assert {"param", "evidence"} <= set(value["required"])
        assert "wanted" not in value["properties"]
        assert "evidence" in state["required"]
        assert "wanted" not in state["properties"]  # [V2.78]: the subgoal declares it
        assert "param" in state["properties"]  # a hint: optional ([V2.39] (2))
        assert "param" not in state["required"]
        assert set(nav["properties"]) == {"subgoal", "kind", "evidence"}
        for one in (value, state):
            evidence = one["properties"]["evidence"]
            assert (evidence["minItems"], evidence["maxItems"]) == (1, 1)


def test_a_value_claim_cannot_leave_out_its_param() -> None:
    value = _claim_variants(LATER)["value"]
    assert not _fits(value, {"subgoal": "s1", "kind": "value", "evidence": [3]})
    assert _fits(value, {"subgoal": "s1", "kind": "value", "evidence": [3], "param": "text"})


def _minimal_claim(kind: str) -> dict[str, object]:
    doc: dict[str, object] = {"subgoal": "s1", "kind": kind}
    if kind in {"value", "state"}:
        doc["evidence"] = [1]
    if kind == "value":
        doc["param"] = "text"
    return doc


def test_every_claim_shape_the_schema_offers_is_one_validate_accepts() -> None:
    variants = _claim_variants(LATER)
    for kind, variant in variants.items():
        doc = _minimal_claim(kind)
        assert _fits(variant, doc), kind
        decision = Decision.model_validate({"claims": [doc]})
        tokens = validate(decision, listed_ids=frozenset({1}), goal=GOAL, first_turn=False,
                          known_subgoals={"s1": frozenset({"text"})})  # fmt: skip
        assert tokens == [], (kind, tokens)


def test_decide_refuses_a_first_turn_that_disagrees_with_the_subgoal_lines() -> None:
    """Principal #417 (2): the schema shown follows the subgoal lines and the reply is validated
    by `first_turn`; a caller where they disagree fails before any call."""
    calls: list[object] = []

    def ask(_system: list[str], _blocks: list[str]) -> str:
        calls.append(None)
        return "{}"

    for first_turn, lines in ((True, ["s1 closes=value commit=none: x"]), (False, [])):
        with pytest.raises(ValueError, match="first_turn_mismatch"):
            decide(ask, task_goal=GOAL, subgoal_lines=lines, action_log=[], subgoal_status=[],
                   window_title="t", listing_rows=[], listed_ids=frozenset(),
                   first_turn=first_turn, task_fence=TASK_FENCE)  # fmt: skip
    assert calls == []


def test_typed_text_is_never_empty() -> None:
    """`validate` reads "" as no text (`text_missing`); the schema does not offer it."""
    assert _variants(LATER)["type"]["properties"]["text"]["minLength"] == 1


def test_every_optional_field_the_schema_offers_is_one_validate_accepts() -> None:
    """Principal #417 note: the maximal shapes too, not only the minimal ones. A commit on every
    verb that may carry one, a param hint on a state claim, cited rows on navigated."""
    for verb, variant in _variants(FIRST).items():
        doc = _minimal(verb)
        if "commit" in variant["properties"]:
            doc["commit"] = "none"
        assert _fits(variant, doc), verb
        decision = Decision.model_validate(
            {
                "subgoals": [
                    {
                        "id": "s1",
                        "desc": "d",
                        "params": {"text": "hello"},
                        "closes": "state",
                        "wanted": "on",
                    }
                ],
                "actions": [doc],
            }
        )
        assert validate(decision, listed_ids=frozenset({1}), goal=GOAL, first_turn=True) == []
    claims = _claim_variants(LATER)
    for kind, extra in (("state", {"param": "text"}), ("navigated", {"evidence": [1]})):
        doc = {**_minimal_claim(kind), **extra}
        assert _fits(claims[kind], doc), kind
        decision = Decision.model_validate({"claims": [doc]})
        tokens = validate(decision, listed_ids=frozenset({1}), goal=GOAL, first_turn=False,
                          known_subgoals={"s1": frozenset({"text"})})  # fmt: skip
        assert tokens == [], (kind, tokens)
