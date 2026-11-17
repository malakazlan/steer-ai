"""[V2.78]: a state subgoal declares its wanted state at intake, before any action; the claim
carries none, and code compares the state it reads with the DECLARED wanted. A state subgoal with
no wanted is `wanted_missing`; a value or page subgoal with one is `wanted_not_state`."""

from __future__ import annotations

import json
from typing import Literal

import pytest
from pydantic import ValidationError

from steerai.loop2.controller import (
    REPAIR_LEGEND,
    SYSTEM_RULES,
    ControllerError,
    _prompt_schema,
    parse,
)
from steerai.loop2.decision import Claim, Decision, SubgoalDoc, validate
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import MENU, PAGE, World, act, element, observed, report, task

BOX = element(42, "CheckBox", "Red", (9, 42), 1)
Wanted = Literal["on", "off"]


def _world(declared: Wanted, *, read_on: bool, read_off: bool) -> World:
    """A toggle the model turned over (or not): the live read of each state is fixed, the
    subgoal declared `declared` at intake, and the claim names no state."""
    subs = [SubgoalDoc(id="s1", desc="set", params={"a": "red"}, closes="state", wanted=declared)]
    world = World(
        [
            Decision(subgoals=subs, actions=[act("click", target=MENU.id)]),
            Decision(claims=[Claim(subgoal="s1", kind="state", evidence=[BOX.id], param="a")]),
        ],
        effects=[report("content_changed")],
        screen=observed([*PAGE, BOX]),
    )
    world.states[(BOX.runtime_id, "on")] = read_on
    world.states[(BOX.runtime_id, "off")] = read_off
    return world


def test_a_toggle_turned_off_by_mistake_is_never_proven_for_a_declared_on() -> None:
    world = _world("on", read_on=False, read_off=True)
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert "claim:s1:state_mismatch" in world.views[-1].history


def test_a_declared_off_that_reads_off_is_proven() -> None:
    world = _world("off", read_on=False, read_off=True)
    assert run_task(task(), world.ports()).status == "done"


def test_a_claim_that_still_sends_wanted_is_not_a_claim() -> None:
    with pytest.raises(ValidationError):
        Claim.model_validate({"subgoal": "s1", "kind": "state", "evidence": [1], "wanted": "on"})
    raw = json.dumps(
        {"claims": [{"subgoal": "s1", "kind": "state", "evidence": [1], "wanted": "on"}]}
    )
    with pytest.raises(ControllerError) as caught:
        parse(raw)
    assert caught.value.code == "schema"


def _first(**sub: object) -> list[str]:
    doc = {"id": "s1", "desc": "d", "params": {"a": "red"}, "closes": "state"} | sub
    decision = Decision(subgoals=[SubgoalDoc.model_validate(doc)], stop="cannot")
    return list(validate(decision, listed_ids=frozenset(), goal="tick red", first_turn=True))


def test_a_state_subgoal_without_wanted_is_wanted_missing() -> None:
    assert _first() == ["wanted_missing"]
    assert _first(wanted="on") == []


@pytest.mark.parametrize("closes", ["value", "page"])
def test_a_value_or_page_subgoal_with_wanted_is_wanted_not_state(closes: str) -> None:
    field = {"field": "unnamed"} if closes == "value" else {}
    assert _first(closes=closes, wanted="on", **field) == ["wanted_not_state"]
    assert _first(closes=closes, **field) == []


def test_the_new_token_is_taught_in_the_legend_and_the_rules() -> None:
    assert "wanted_not_state" in REPAIR_LEGEND
    assert "wanted" in SYSTEM_RULES
    schema = _prompt_schema(first_turn=True)
    assert "wanted" in schema["$defs"]["SubgoalDoc"]["properties"]
    claim_state = schema["$defs"]["Claim_state"]
    assert "wanted" not in claim_state["properties"]
    assert "wanted" not in claim_state["required"]
