"""[V2.65]: the shape errors are made impossible by the schema the prompt shows, not refused after.
The first turn's schema has the subgoals field and later turns' does not; an action is a union by
verb, each variant holding only its verb's fields (wait and press have no target). Later turns
share one static prefix, so caching is unaffected. `validate` stays the backstop."""

from __future__ import annotations

import json
from typing import Any, get_args

from steerai.loop2.controller import PROMPT_VERSION, build_prompt
from steerai.loop2.decision import (
    NO_COMMIT_VERBS,
    STEP,
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
    assert PROMPT_VERSION == "controller-9"


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
                    {"id": "s1", "desc": "d", "params": {"text": "hello"}, "closes": "state"}
                ],
                "actions": [doc],
            }
        )
        assert validate(decision, listed_ids=frozenset({1}), goal=GOAL, first_turn=True) == []
