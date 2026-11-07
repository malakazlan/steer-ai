"""`present` at the plan boundary (#124, 5.10 [v2.36], 6.3): the expect of an R0/R1 click
that names the clicked target, in a landmark; the condition of that click's one-step subgoal;
never inside `all`/`any_of` (hunter PS-03/04/05/06/07/39, principal items 3/4)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.planner.mirror import PredicateDoc
from steerai.verify.predicates import Present

PRESENT: dict[str, Any] = {
    "present": {"role": "Text", "name": "Amir Khan", "within": {"landmark": "Steer probe inbox"}}
}


def _doc() -> dict[str, Any]:
    return {
        "goal": "open the chat with Amir Khan",
        "subgoals": [{"id": "g1", "desc": "the chat is open", "condition": copy.deepcopy(PRESENT)}],
        "steps": [
            {
                "id": "s1",
                "subgoal": "g1",
                "verb": "click",
                "target": {"role": "ListItem", "name": "Amir Khan", "match": "person"},
                "risk": "R1",
                "expect": copy.deepcopy(PRESENT),
            }
        ],
    }


def _load(doc: dict[str, Any]) -> Any:
    return plan_from_json(json.dumps(doc))


def _refused(doc: dict[str, Any], path: str) -> str:
    with pytest.raises(PlanError) as caught:
        _load(doc)
    assert caught.value.path.startswith(path), caught.value
    return str(caught.value)


def test_a_selection_click_and_its_subgoal_parse_and_round_trip() -> None:
    plan = _load(_doc())
    assert isinstance(plan.steps[0].expect, Present)
    assert plan.subgoals[0].condition == plan.steps[0].expect
    assert _load(json.loads(plan_to_json(plan))) == plan


def test_present_needs_a_landmark() -> None:
    doc = _doc()
    del doc["steps"][0]["expect"]["present"]["within"]
    _refused(doc, "$.steps[0].expect.present")


def test_present_takes_only_its_keys() -> None:
    doc = _doc()
    doc["steps"][0]["expect"]["present"]["expected"] = 2
    _refused(doc, "$.steps[0].expect.present")


def test_present_is_the_expect_of_a_click_only() -> None:
    doc = _doc()
    doc["steps"][0]["verb"] = "focus"
    doc["steps"][0]["target"] = {"role": "Edit", "name": "Amir Khan"}  # a valid focus otherwise
    message = _refused(doc, "$.steps[0].expect")
    assert "click" in message


def test_present_is_never_an_r2_or_r3_expect() -> None:
    for risk in ("R2", "R3"):
        doc = _doc()
        doc["steps"][0]["risk"] = risk
        _refused(doc, "$.steps[0].expect")


def test_present_names_the_clicked_target() -> None:
    doc = _doc()
    for where in (doc["steps"][0]["expect"], doc["subgoals"][0]["condition"]):
        where["present"]["name"] = "Conversation"
    message = _refused(doc, "$.steps[0].expect")
    assert "target" in message


def test_present_never_names_its_landmark() -> None:
    doc = _doc()
    doc["steps"][0]["target"]["name"] = "Steer probe inbox"
    for where in (doc["steps"][0]["expect"], doc["subgoals"][0]["condition"]):
        where["present"]["name"] = "Steer probe inbox"
    _refused(doc, "$.steps[0].expect")


def test_present_is_never_inside_all_or_any_of() -> None:
    for kind in ("all", "any_of"):
        doc = _doc()
        other = {"title": {"pattern": "Amir Khan"}}
        doc["subgoals"][0]["condition"] = {kind: [copy.deepcopy(PRESENT), other]}
        _refused(doc, "$.subgoals[0].condition")
        doc = _doc()
        doc["steps"][0]["expect"] = {kind: [copy.deepcopy(PRESENT), other]}
        _refused(doc, "$.steps[0].expect")


def test_a_present_condition_belongs_to_one_click_with_that_expect() -> None:
    doc = _doc()
    doc["steps"][0]["expect"] = {"title": {"pattern": "Amir Khan"}}
    _refused(doc, "$.subgoals[0].condition")
    doc = _doc()
    doc["steps"].append(
        {
            "id": "s2",
            "subgoal": "g1",
            "verb": "focus",
            "target": {"role": "Edit", "name": "Message"},
            "risk": "R0",
        }
    )
    _refused(doc, "$.subgoals[0].condition")
    doc = _doc()
    doc["steps"][0]["subgoal"] = "g2"
    doc["subgoals"].append(
        {"id": "g2", "desc": "other", "condition": {"title": {"pattern": "Amir Khan"}}}
    )
    _refused(doc, "$.subgoals[0].condition")  # a step-less present subgoal


def test_the_mirror_keeps_the_landmark_of_a_present() -> None:
    doc = PredicateDoc(kind="present", role="Text", name="Amir Khan", within="Steer probe inbox")
    assert doc.to_file_form() == {
        "present": {
            "role": "Text",
            "name": "Amir Khan",
            "within": {"landmark": "Steer probe inbox"},
        }
    }


def _with_send(*, scope: str | None, within: str | None) -> dict[str, Any]:
    doc = _doc()
    doc["goal"] = "reply ok thanks to Amir Khan"
    appeared: dict[str, Any] = {"role": "Text", "name": "ok thanks"}
    if within is not None:
        appeared["within"] = {"landmark": within}
    params: dict[str, Any] = {
        "text": "ok thanks",
        "composer_role": "Edit",
        "composer_name": "Message",
    }
    if scope is not None:
        params["scope"] = scope
    doc["subgoals"].append({"id": "g2", "desc": "sent", "condition": {"appeared": appeared}})
    doc["steps"].append(
        {
            "id": "s2",
            "subgoal": "g2",
            "verb": "send",
            "target": {"role": "Button", "name": "Send"},
            "risk": "R2",
            "params": params,
            "expect": {"appeared": copy.deepcopy(appeared)},
        }
    )
    return doc


def test_an_irreversible_step_after_a_selection_names_where_its_content_lands() -> None:
    """With no content landmark nothing is excluded from the present: a message in the wrong
    chat reading exactly the name would prove it (principal re-review, blocking 1)."""
    message = _refused(_with_send(scope=None, within=None), "$.steps[1]")
    assert "content lands" in message
    _load(_with_send(scope="Conversation", within=None))
    _load(_with_send(scope=None, within="Conversation"))


def test_a_plan_without_a_present_needs_no_content_landmark() -> None:
    doc = _with_send(scope=None, within=None)
    doc["subgoals"][0]["condition"] = {"title": {"pattern": "Amir Khan"}}
    doc["steps"][0]["expect"] = {"title": {"pattern": "Amir Khan"}}
    _load(doc)
