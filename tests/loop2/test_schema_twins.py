"""[V2.68] Two: the first-turn schema states the validator's own limits, never stricter. Each
schema constraint below has its `validate` twin (the same constant, and the same verdict at the
boundary), and each of those validator limits has its schema constraint."""

from __future__ import annotations

import re
from typing import Any

import pytest

from steerai.loop2.controller import _prompt_schema
from steerai.loop2.decision import (
    MAX_ACTIONS,
    MAX_DESC_CHARS,
    MAX_SUBGOALS,
    PARAM_NAME,
    SUBGOAL_ID,
    ActionDoc,
    Decision,
    SubgoalDoc,
    validate,
)

GOAL = "find red shoes"


def _ecma(pattern: str, text: str) -> bool:
    r"""A JSON Schema pattern as ECMA-262 reads it: its `$` never matches before a final
    newline, as Python's does, so it is read here as an end-of-text anchor."""
    assert pattern.endswith("$")
    return re.search(pattern[:-1] + r"\Z", text) is not None


def _schema(first: bool = True) -> dict[str, Any]:  # Any: a JSON schema document
    return _prompt_schema(first_turn=first)


def _tokens(decision: Decision) -> list[str]:
    return list(validate(decision, listed_ids=frozenset({1}), goal=GOAL, first_turn=True))


def _sub(**changes: Any) -> SubgoalDoc:  # Any: test values
    base: dict[str, Any] = {"id": "s1", "desc": "the shoes", "closes": "value",
                            "params": {"q": "red shoes"}}  # fmt: skip
    return SubgoalDoc(**{**base, **changes})


def _first(*subs: SubgoalDoc, actions: int = 1) -> Decision:
    acts = [ActionDoc(verb="click", target=1, subgoal="s1") for _ in range(actions)]
    return Decision(subgoals=list(subs), actions=acts)


@pytest.mark.parametrize("sid", ["s1", "a" * 16, "a" * 17, "S1", "s-1", "", "s1\n"])
def test_the_subgoal_id_pattern_is_the_validators(sid: str) -> None:
    schema = _schema()
    assert schema["$defs"]["SubgoalDoc"]["properties"]["id"] == {"$ref": "#/$defs/SubgoalId"}
    pattern = schema["$defs"]["SubgoalId"]["pattern"]
    schema_ok = _ecma(pattern, sid)
    assert schema_ok == (SUBGOAL_ID.fullmatch(sid) is not None)
    assert schema_ok == ("subgoal_id_bad" not in _tokens(_first(_sub(id=sid))))


def test_every_subgoal_reference_carries_the_id_pattern() -> None:
    """A reference that fails the pattern can never be a declared id (`subgoal_unknown`)."""
    for first in (True, False):
        schema = _schema(first)
        assert schema["$defs"]["SubgoalId"]["pattern"] == f"^{SUBGOAL_ID.pattern}$"
        for name, part in schema["$defs"].items():
            if name.startswith(("Action_", "Claim_")):
                assert part["properties"]["subgoal"] == {"$ref": "#/$defs/SubgoalId"}, name


@pytest.mark.parametrize("name", ["q", "a" * 24, "a" * 25, "Q", "two words", ""])
def test_the_param_name_pattern_is_the_validators(name: str) -> None:
    params = _schema()["$defs"]["SubgoalDoc"]["properties"]["params"]
    schema_ok = _ecma(params["propertyNames"]["pattern"], name)
    assert schema_ok == (PARAM_NAME.fullmatch(name) is not None)
    got = _tokens(_first(_sub(params={name: "red shoes"})))
    assert schema_ok == ("param_name_bad" not in got)


def test_an_empty_param_value_is_refused_by_both() -> None:
    params = _schema()["$defs"]["SubgoalDoc"]["properties"]["params"]
    assert params["additionalProperties"]["minLength"] == 1
    assert "param_not_span" in _tokens(_first(_sub(params={"q": ""})))


@pytest.mark.parametrize("size", [MAX_DESC_CHARS, MAX_DESC_CHARS + 1])
def test_the_desc_length_is_the_validators(size: str | int) -> None:
    desc = _schema()["$defs"]["SubgoalDoc"]["properties"]["desc"]
    assert desc["maxLength"] == MAX_DESC_CHARS
    got = _tokens(_first(_sub(desc="d" * int(size))))
    assert ("desc_bad" in got) == (int(size) > desc["maxLength"])


@pytest.mark.parametrize("count", [MAX_ACTIONS, MAX_ACTIONS + 1])
def test_the_action_count_is_the_validators(count: int) -> None:
    assert _schema()["properties"]["actions"]["maxItems"] == MAX_ACTIONS
    got = _tokens(_first(_sub(), actions=count))
    assert ("too_many_actions" in got) == (count > MAX_ACTIONS)


@pytest.mark.parametrize("count", [MAX_SUBGOALS, MAX_SUBGOALS + 1])
def test_the_subgoal_count_is_the_validators(count: int) -> None:
    subgoals = _schema()["properties"]["subgoals"]
    branch = next(b for b in subgoals["anyOf"] if b.get("type") == "array")
    assert branch["maxItems"] == MAX_SUBGOALS
    subs = [_sub(id=f"s{i}") for i in range(1, count + 1)]
    assert ("too_many_subgoals" in _tokens(_first(*subs))) == (count > MAX_SUBGOALS)


def test_subgoals_are_not_required_since_a_first_ask_or_stop_needs_none() -> None:
    """Never stricter than `validate`: a first reply that only asks or stops is valid."""
    schema = _schema()
    assert "subgoals" not in schema.get("required", [])
    assert _tokens(Decision(ask="which size?")) == []
    assert _tokens(Decision(stop="cannot")) == []


def test_an_empty_ask_is_refused_by_both() -> None:
    ask = _schema()["properties"]["ask"]
    branch = next(b for b in ask["anyOf"] if b.get("type") == "string")
    assert branch["minLength"] == 1
    assert "ask_empty" in _tokens(Decision(ask=""))


def test_the_pattern_literals_are_pinned_to_their_length_constants() -> None:
    """decision.py is a token producer: its patterns stay literals (no string built at
    runtime), and their bounds are the constants the schema states."""
    from steerai.loop2.decision import PARAM_NAME_CHARS, SUBGOAL_ID_CHARS  # noqa: PLC0415

    assert SUBGOAL_ID.pattern == "[a-z0-9_]{1,%d}" % SUBGOAL_ID_CHARS  # noqa: UP031 - a pin
    assert PARAM_NAME.pattern == "[a-z0-9_]{1,%d}" % PARAM_NAME_CHARS  # noqa: UP031 - a pin


@pytest.mark.parametrize("closes", ["value", "state", "page"])
def test_a_subgoal_with_no_param_is_refused_by_both(closes: str) -> None:
    """[V2.68] Two ruling (b): every closing kind needs a param; the schema says so once."""
    params = _schema()["$defs"]["SubgoalDoc"]["properties"]["params"]
    assert params["minProperties"] == 1
    got = _tokens(_first(_sub(closes=closes, params={})))
    assert f"{closes}_without_param" in got
