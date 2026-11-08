"""Refusals the gate always makes at run time move to the plan boundary, where the planner's
one repair turn (5.3 [v2.38]) can fix them (5.3 [v2.41]; live 2026-11-08, Wikipedia: a click with
no expect blocked `no_predicate`; a submit scoped by its own text blocked
`pattern_is_the_scope`)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from steerai.orchestrator.plan_io import PlanError, check_runnable, plan_from_json

SUBMIT: dict[str, Any] = {
    "id": "s2",
    "subgoal": "g1",
    "verb": "submit",
    "target": {"role": "Edit", "name": "Search Wikipedia"},
    "risk": "R2",
    "params": {
        "text": "Alan Turing",
        "composer_role": "Edit",
        "composer_name": "Search Wikipedia",
        "scope": "Wikipedia",
    },
    "expect": {
        "appeared": {
            "role": "Document",
            "name": "Alan Turing",
            "decoration": "wikipedia",
            "replaces": True,
        }
    },
}


def _doc(*steps: dict[str, Any]) -> dict[str, Any]:
    return {
        "goal": "search Wikipedia for Alan Turing",
        "subgoals": [
            {"id": "g1", "desc": "article shown", "condition": copy.deepcopy(SUBMIT["expect"])}
        ],
        "steps": [copy.deepcopy(s) for s in steps],
    }


def _refused(doc: dict[str, Any]) -> PlanError:
    plan = plan_from_json(json.dumps(doc))  # the codec still loads hand-written plans
    with pytest.raises(PlanError) as caught:
        check_runnable(plan)
    return caught.value


def test_a_click_without_an_expect_is_refused_at_its_path() -> None:
    click = {
        "id": "s1",
        "subgoal": "g1",
        "verb": "click",
        "target": {"role": "Hyperlink", "name": "Search"},
        "risk": "R0",
    }
    error = _refused(_doc(click, SUBMIT))
    assert error.path == "$.steps[0].expect"
    assert "click" in str(error)


def test_a_send_or_submit_scoped_by_its_own_text_is_refused() -> None:
    for verb in ("submit", "send"):
        step = copy.deepcopy(SUBMIT)
        step["verb"] = verb
        step["params"]["scope"] = " alan  TURING "
        error = _refused(_doc(step))
        assert error.path == "$.steps[0].params.scope"


def test_a_well_formed_search_plan_is_runnable() -> None:
    check_runnable(plan_from_json(json.dumps(_doc(SUBMIT))))
