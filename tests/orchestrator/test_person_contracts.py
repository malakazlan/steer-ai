"""The `match` mode of a target (design 5.8 [v1.5]): validated in the contract, part of the
action key's identity, encoded by the codec only when it is `person`."""

from __future__ import annotations

import json

import pytest

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.ledger import action_key
from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from tests.orchestrator.test_plan_io import _doc


def test_match_defaults_to_exact_and_accepts_person_only() -> None:
    assert Target(role="ListItem", name="Amir").match == "exact"
    assert Target(role="ListItem", name="Amir", match="person").match == "person"
    with pytest.raises(ValueError, match="match"):
        Target(role="ListItem", name="Amir", match="fuzzy")  # type: ignore[arg-type]


def test_the_identity_and_the_action_key_carry_the_mode() -> None:
    exact = Target(role="ListItem", name="Amir")
    person = Target(role="ListItem", name="Amir", match="person")
    assert exact.identity != person.identity
    assert exact.identity.endswith("|exact")
    assert person.identity.endswith("|person")
    secret = b"s" * 32
    assert action_key(secret, "click", exact, {}) != action_key(secret, "click", person, {})


def test_the_codec_reads_and_writes_person_targets_and_omits_exact() -> None:
    doc = _doc()
    doc["steps"][0]["target"] = {"role": "ListItem", "name": "Amir", "match": "person"}
    plan = plan_from_json(json.dumps(doc))
    target = plan.steps[0].target
    assert target is not None
    assert target.match == "person"
    text = plan_to_json(plan)
    assert text.count('"match": "person"') == 1
    assert '"match": "exact"' not in text
    assert plan_from_json(text) == plan


def test_the_codec_refuses_an_unknown_match_mode() -> None:
    doc = _doc()
    doc["steps"][0]["target"] = {"role": "ListItem", "name": "Amir", "match": "fuzzy"}
    with pytest.raises(PlanError, match=r"\$\.steps\[0\]\.target\.match"):
        plan_from_json(json.dumps(doc))


def test_a_person_target_still_needs_a_name() -> None:
    with pytest.raises(ValueError, match="name"):
        Target(role="ListItem", name="   ", match="person")
    step = Step(
        id="s",
        subgoal="g1",
        verb="click",
        target=Target(role="ListItem", name="Amir", match="person"),
        risk="R1",
    )
    assert step.target is not None
    assert step.target.match == "person"
