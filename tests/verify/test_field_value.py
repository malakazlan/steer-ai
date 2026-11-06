"""The `value` subgoal condition (issue #36; packet field-value-2026-11-06, schema gate REVISE 7;
hunter FV-02/05/06/07/08/38/45/48): a field holds exactly the text a step typed."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.action.physical import TEXT_ROLES
from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.verify.predicates import FIELD_ROLES, FieldValue
from steerai.verify.t1 import redact

SENTINEL = "sentinel-field-text-5b1c"


def test_the_field_roles_are_the_text_roles() -> None:
    """Gate item 3: on any other role the read returns the element's own name (FV-02)."""
    assert FIELD_ROLES == TEXT_ROLES


@pytest.mark.parametrize("role", ["Text", "Button", "Hyperlink", ""])
def test_a_non_field_role_is_refused(role: str) -> None:
    with pytest.raises(ValueError, match="role"):
        FieldValue(role=role, name_pattern="Text Editor", text="hello")


@pytest.mark.parametrize("text", ["", "   ", "\t"])
def test_empty_text_is_refused(text: str) -> None:
    """Gate item 2: an unreadable value reads as "" and would hold (FV-06)."""
    with pytest.raises(ValueError, match="text"):
        FieldValue(role="Document", name_pattern="Text Editor", text=text)


def test_text_over_the_type_cap_is_refused() -> None:
    """FV-38: a condition no `type` step can reach."""
    with pytest.raises(ValueError, match="text"):
        FieldValue(role="Edit", name_pattern="Search", text="x" * 501)


def test_redaction_keeps_the_role_only() -> None:
    """Gate item 6 / FV-08."""
    spec = redact(FieldValue(role="Document", name_pattern=SENTINEL, text=SENTINEL))
    assert spec == {"kind": "FieldValue", "role": "Document"}


# ---- the plan file ------------------------------------------------------------------------------


def _doc(condition: dict[str, Any], *, expect: dict[str, Any] | None = None) -> str:
    step: dict[str, Any] = {
        "id": "s1",
        "subgoal": "g1",
        "verb": "type",
        "target": {"role": "Document", "name": "Text Editor"},
        "text": "hello",
        "risk": "R1",
    }
    if expect is not None:
        step["expect"] = expect
    return json.dumps(
        {
            "goal": "type hello in notepad",
            "subgoals": [{"id": "g1", "desc": "hello is in notepad", "condition": condition}],
            "steps": [step],
        }
    )


VALUE = {"value": {"role": "Document", "name": "Text Editor", "text": "hello"}}


def test_a_value_condition_round_trips() -> None:
    plan = plan_from_json(_doc(VALUE))
    assert plan.subgoals[0].condition == FieldValue(
        role="Document", name_pattern="Text Editor", text="hello"
    )
    assert plan_from_json(plan_to_json(plan)).subgoals[0].condition == plan.subgoals[0].condition


@pytest.mark.parametrize(
    "expect",
    [
        VALUE,
        {"all": [{"title": {"pattern": "x"}}, VALUE]},
        {"any_of": [{"title": {"pattern": "x"}}, {"all": [VALUE]}]},
    ],
)
def test_value_is_never_a_step_expect_at_any_depth(expect: dict[str, Any]) -> None:
    """Gate item 5 / FV-07: presence with no before value cannot prove a step's effect."""
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc({"title": {"pattern": "x"}}, expect=expect))
    assert caught.value.path.startswith("$.steps[0].expect")
    assert caught.value.path.endswith(".value")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"role": "Text", "name": "x", "text": "hello"}, "role"),
        ({"role": "Document", "name": "Text Editor", "text": ""}, "text"),
        ({"role": "Document", "name": "Text Editor", "text": 3}, "text"),
        ({"role": "Document", "name": "", "text": "hello"}, "name"),
        ({"role": "Document", "name": "Text Editor", "text": "hello", "within": "x"}, None),
    ],
)
def test_a_bad_value_body_is_refused_at_its_path(body: dict[str, Any], field: str | None) -> None:
    """FV-45."""
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc({"value": body}))
    assert caught.value.path.startswith("$.subgoals[0].condition.value")
    if field is not None:
        assert field in caught.value.path or field in str(caught.value)


def test_the_live_kind_keeps_its_own_refusal() -> None:
    """FV-48: `value_equals` still needs a snapshot element id."""
    with pytest.raises(PlanError, match="snapshot element id"):
        plan_from_json(_doc({"value_equals": {"target_id": 1, "text": "x"}}))
