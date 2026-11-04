"""The pydantic mirror of the 6.3 file form must never allow a key, verb, risk, kind or
decoration the plan boundary refuses, and must leave out exactly the gate's own inputs
(packet planner-2026-11-04, decision Q3)."""

from __future__ import annotations

import json
from typing import Any

from anthropic.lib._parse._transform import transform_schema

from steerai.orchestrator.contracts import RISKS
from steerai.orchestrator.plan_io import (
    APPEARED_KEYS,
    FILE_VERBS,
    KINDS,
    PARAM_KEYS,
    STEP_KEYS,
    TARGET_KEYS,
    plan_from_json,
    plan_to_json,
)
from steerai.planner.mirror import (
    MODEL_OWNED_STEP_KEYS,
    AppearedDoc,
    ParamsDoc,
    PlanDoc,
    PredicateDoc,
    StepDoc,
    SubgoalDoc,
    TargetDoc,
)
from steerai.verify.decorations import DECORATIONS

EXPECTED_ABSENT = frozenset({"preapproved_by_request", "text_provenance", "path"})
"""Gate and mechanism inputs the model never writes: provenance and preapproval are the gate's
(5.13), `path` chooses the executor and is the loop's."""


def _schema() -> dict[str, Any]:
    return transform_schema(PlanDoc.model_json_schema())


def _definition(schema: dict[str, Any], name: str) -> dict[str, Any]:
    defs = schema.get("$defs", {})
    assert name in defs, sorted(defs)
    return dict(defs[name])


def _resolved(schema: dict[str, Any], option: dict[str, Any]) -> dict[str, Any]:
    ref = option.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return _definition(schema, ref.removeprefix("#/$defs/"))
    return option


def _properties(definition: dict[str, Any]) -> frozenset[str]:
    return frozenset(definition.get("properties", {}))


def test_every_object_allows_a_subset_of_the_boundarys_keys() -> None:
    schema = _schema()
    assert _properties(schema) == frozenset({"goal", "subgoals", "steps"})
    assert _properties(_definition(schema, "SubgoalDoc")) == frozenset({"id", "desc", "condition"})
    assert _properties(_definition(schema, "StepDoc")) <= STEP_KEYS
    assert _properties(_definition(schema, "TargetDoc")) <= TARGET_KEYS
    assert _properties(_definition(schema, "ParamsDoc")) <= PARAM_KEYS
    assert _properties(_definition(schema, "AppearedDoc")) <= APPEARED_KEYS
    assert _properties(_definition(schema, "PredicateDoc")) <= KINDS
    assert _properties(_definition(schema, "LeafPredicateDoc")) <= KINDS - {"all", "any_of"}


def test_the_absent_step_keys_are_exactly_the_gates_inputs() -> None:
    schema = _schema()
    absent = STEP_KEYS - _properties(_definition(schema, "StepDoc"))
    assert absent == EXPECTED_ABSENT
    assert MODEL_OWNED_STEP_KEYS == STEP_KEYS - EXPECTED_ABSENT


def test_every_enum_is_a_subset_of_the_boundarys_sets() -> None:
    schema = _schema()
    step = _definition(schema, "StepDoc")["properties"]
    assert frozenset(step["verb"]["enum"]) <= FILE_VERBS
    assert frozenset(step["risk"]["enum"]) == frozenset(RISKS)
    appeared = _definition(schema, "AppearedDoc")["properties"]
    decoration = appeared["decoration"]
    options = [_resolved(schema, o) for o in decoration.get("anyOf", [decoration])]
    enums = {v for option in options for v in option.get("enum", [])}
    assert enums == frozenset(DECORATIONS), "the API must constrain the key, not describe it"
    target = _definition(schema, "TargetDoc")["properties"]
    match_options = target["match"].get("anyOf", [target["match"]])
    assert {v for o in match_options for v in o.get("enum", [])} == {"exact", "person"}


def test_no_object_accepts_additional_properties() -> None:
    schema = _schema()
    assert schema.get("additionalProperties") is False
    for name, definition in schema.get("$defs", {}).items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name


def test_a_fixture_mirror_round_trips_through_the_boundary() -> None:
    condition = PredicateDoc(appeared=AppearedDoc(role="Text", name="hello"))
    doc = PlanDoc(
        goal="send hello",
        subgoals=[SubgoalDoc(id="g1", desc="sent", condition=condition)],
        steps=[
            StepDoc(
                id="s1",
                subgoal="g1",
                verb="type",
                target=TargetDoc(role="Edit", name="Message"),
                risk="R1",
                text="hello",
            ),
            StepDoc(
                id="s2",
                subgoal="g1",
                verb="send",
                target=TargetDoc(role="Button", name="Send", automation_id="btnSend"),
                risk="R2",
                params=ParamsDoc(
                    text="hello",
                    composer_role="Edit",
                    composer_name="Message",
                    scope="Conversation",
                ),
            ),
        ],
    )
    text = json.dumps(doc.model_dump(mode="json", exclude_unset=True))
    plan = plan_from_json(text)
    assert plan.steps[0].expect is None
    assert plan.steps[0].target is not None
    assert plan.steps[0].target.automation_id == ""
    assert json.loads(plan_to_json(plan))["steps"][1]["params"]["scope"] == "Conversation"
    assert plan_from_json(plan_to_json(plan)) == plan


def test_the_mirror_refuses_the_gates_inputs_from_the_model() -> None:
    """A model that writes provenance or preapproval is rejected by the schema, not trusted."""
    import pytest  # noqa: PLC0415
    from pydantic import ValidationError  # noqa: PLC0415

    base = {
        "id": "s1",
        "subgoal": "g1",
        "verb": "type",
        "target": {"role": "Edit", "name": "Message"},
        "risk": "R1",
        "text": "hello",
    }
    for key in sorted(EXPECTED_ABSENT):
        with pytest.raises(ValidationError, match=key):
            StepDoc.model_validate({**base, key: "verbatim"})
