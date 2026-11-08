"""`window` is an enum in the model's schema (5.3 [v2.42]): live 2026-11-08 the model wrote
`window = "start','expect_placeholder"` on Wikipedia, on the first call and again after the
repair turn (a free string field took a malformed value). Now the model picks `start` or
`opened` and names the open step apart; the mirror writes the file form `opened:<id>`."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from steerai.orchestrator.plan_io import STEP_KEYS
from steerai.planner.mirror import (
    MIRROR_ONLY_STEP_KEYS,
    PlanDoc,
    PredicateDoc,
    StepDoc,
    SubgoalDoc,
    TargetDoc,
)


def _doc(**window: Any) -> PlanDoc:
    return PlanDoc(
        goal="g",
        subgoals=[SubgoalDoc(id="g1", desc="d", condition=PredicateDoc(kind="title", pattern="x"))],
        steps=[
            StepDoc(
                id="s2",
                subgoal="g1",
                verb="click",
                target=TargetDoc(role="Button", name="Ok"),
                risk="R0",
                expect=PredicateDoc(kind="title", pattern="x"),
                **window,
            )
        ],
    )


def _window(**window: Any) -> object:
    return _doc(**window).to_file_form()["steps"][0].get("window")


def test_the_schema_offers_only_start_and_opened() -> None:
    schema = PlanDoc.model_json_schema()
    step = schema["$defs"]["StepDoc"]["properties"]["window"]
    options = [
        schema["$defs"].get(o["$ref"].split("/")[-1], o) if "$ref" in o else o
        for o in step.get("anyOf", [step])
    ]
    assert {v for o in options for v in o.get("enum", [])} == {"start", "opened"}


def test_the_file_form_is_the_reference() -> None:
    assert _window() is None
    assert _window(window="start") == "start"
    assert _window(window="opened", opened_step="s1") == "opened:s1"
    assert _window(opened_step="s1") == "opened:s1", "the id alone says which open"
    assert _window(window="opened") == "opened:", "no id: the boundary refuses it at its path"
    assert "opened_step" not in _doc(window="opened", opened_step="s1").to_file_form()["steps"][0]


def test_a_malformed_window_cannot_be_written() -> None:
    with pytest.raises(ValidationError):
        _doc(window="start','expect_placeholder")


def test_the_mirror_only_key_never_reaches_the_boundary() -> None:
    assert frozenset({"opened_step"}) == MIRROR_ONLY_STEP_KEYS
    assert not MIRROR_ONLY_STEP_KEYS & STEP_KEYS
