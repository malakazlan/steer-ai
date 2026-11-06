"""The pydantic mirror of the 6.3 file form must never allow a key, verb, risk, kind or
decoration the plan boundary refuses, and must leave out exactly the gate's own inputs
(packet planner-2026-11-04, decision Q3)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from anthropic.lib._parse._transform import transform_schema

from steerai.orchestrator.contracts import RISKS
from steerai.orchestrator.plan_io import (
    FILE_VERBS,
    KINDS,
    PARAM_KEYS,
    PREDICATE_KEYS,
    STEP_KEYS,
    TARGET_KEYS,
    plan_from_json,
    plan_to_json,
)
from steerai.planner.mirror import (
    MODEL_OWNED_STEP_KEYS,
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
    predicate = _definition(schema, "PredicateDoc")["properties"]
    # one kind set: a second predicate schema made the grammar too large for the API (HTTP 400,
    # 2026-11-06); `value` under an expect is refused by the boundary at its path instead
    assert frozenset(predicate["kind"]["enum"]) == KINDS - {"all", "any_of"}


def test_every_flat_predicate_translates_to_a_subset_of_the_boundarys_keys() -> None:
    """The flat predicate's file form never carries a key the boundary refuses for that kind."""
    full = {
        "role": "Text",
        "name": "hello",
        "within": "Chats",
        "pattern": "x",
        "delta": -1,
        "expected": 2,
        "order": "prepend",
        "decoration": "google_search",
        "replaces": True,
        "text": "hello",
    }
    assert set(PREDICATE_KEYS) == KINDS - {"all", "any_of"}
    for kind, keys in PREDICATE_KEYS.items():
        # the test fills every field; `kind` is one of the Literal's values at run time
        form = PredicateDoc(kind=kind, **full).to_file_form()  # type: ignore[arg-type]
        assert set(form) == {kind}
        assert frozenset(form[kind]) <= keys, kind


@pytest.mark.parametrize(
    "predicate",
    [
        PredicateDoc(kind="appeared", role="Text", name="hello", within="chats", expected=2),
        PredicateDoc(kind="disappeared", role="ListItem", name="Amir", within="chats"),
        PredicateDoc(kind="count_delta", role="ListItem", delta=-1, within="chats"),
        PredicateDoc(kind="title", pattern="Amir - Chat"),
        PredicateDoc(kind="window_exists", name="Save As"),
        PredicateDoc(kind="window_gone", name="Save As"),
    ],
)
def test_every_kind_round_trips_through_the_boundary(predicate: PredicateDoc) -> None:
    doc = _doc_with(predicate)
    plan = plan_from_json(json.dumps(doc.to_file_form()))
    assert json.loads(plan_to_json(plan))["subgoals"][0]["condition"] == predicate.to_file_form()


def _doc_with(condition: PredicateDoc) -> PlanDoc:
    return PlanDoc(
        goal="g",
        subgoals=[SubgoalDoc(id="g1", desc="d", condition=condition)],
        steps=[
            StepDoc(
                id="s1",
                subgoal="g1",
                verb="click",
                target=TargetDoc(role="Button", name="Ok"),
                risk="R0",
            )
        ],
    )


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
    predicate = _definition(schema, "PredicateDoc")["properties"]
    decoration = predicate["decoration"]
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
    condition = PredicateDoc(kind="appeared", role="Text", name="hello")
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
    text = json.dumps(doc.to_file_form())
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


def test_the_site_enum_is_the_registry() -> None:
    """The model picks a site key the API constrains; the keys are the registry's (P2)."""
    from steerai.action.launch import SITES  # noqa: PLC0415

    schema = _schema()
    params = _definition(schema, "ParamsDoc")["properties"]
    options = [_resolved(schema, o) for o in params["site"].get("anyOf", [params["site"]])]
    assert {v for o in options for v in o.get("enum", [])} == set(SITES)
    step = _definition(schema, "StepDoc")["properties"]
    assert "open" in step["verb"]["enum"]


def test_site_comes_before_text_in_the_params() -> None:
    """Measured live 2026-11-06: with `text` first the model wrote it before choosing the site
    and left it null (2 of 2 calls); with `site` first it filled the search words 3 of 3."""
    properties = list(_definition(_schema(), "ParamsDoc")["properties"])
    assert properties.index("site") < properties.index("text")
