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


# ---- plan rules (principal rulings on FV-01/03/21/23/25/42) -------------------------------------


def _plan_doc(steps: list[dict[str, Any]], condition: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "goal": "type hello in notepad",
            "subgoals": [
                {"id": "g1", "desc": "hello is in notepad", "condition": condition or VALUE}
            ],
            "steps": steps,
        }
    )


def _type_step(step_id: str = "s1", text: str = "hello", **kw: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": step_id,
        "subgoal": "g1",
        "verb": "type",
        "target": {"role": "Document", "name": kw.pop("name", "Text Editor")},
        "text": text,
        "risk": "R1",
    }
    step.update(kw)
    return step


def test_a_value_subgoal_matching_its_type_step_is_accepted() -> None:
    plan = plan_from_json(_plan_doc([_type_step(name="text editor")]))
    assert plan.steps[0].text == "hello"


def test_a_value_subgoal_without_steps_is_allowed() -> None:
    """Plan 1 lists every subgoal of the goal (5.3 [v2.1]); it cannot tick without its step."""
    plan = json.loads(_plan_doc([]))
    plan["subgoals"].append({"id": "g0", "desc": "d", "condition": {"title": {"pattern": "x"}}})
    plan["steps"] = [{**_type_step(), "subgoal": "g0"}]
    assert plan_from_json(json.dumps(plan)).subgoals[0].id == "g1"


@pytest.mark.parametrize(
    "step",
    [
        _type_step(text="Hello"),  # FV-25: exact text
        _type_step(name="Search"),  # another field
        {**_type_step(), "verb": "focus", "text": None},  # no type step at all
    ],
)
def test_a_value_subgoal_must_name_a_type_step_of_its_own(step: dict[str, Any]) -> None:
    """FV-03/42: the text was typed by this subgoal, never found or guessed."""
    if step.get("text") is None:
        step = {k: v for k, v in step.items() if k != "text"}
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan_doc([step]))
    assert caught.value.path == "$.subgoals[0].condition"


@pytest.mark.parametrize("verb", ["send", "submit"])
def test_a_value_subgoal_owns_no_send_or_submit(verb: str) -> None:
    """FV-01: the value holds after the type, before the effect the subgoal is for."""
    other = {
        "id": "s2",
        "subgoal": "g1",
        "verb": verb,
        "target": {"role": "Button", "name": "Send"},
        "risk": "R2",
        "params": {
            "text": "hello",
            "composer_role": "Document",
            "composer_name": "Text Editor",
            "scope": "Conversation",
        },
        "expect": {"appeared": {"role": "Text", "name": "hello"}},
    }
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan_doc([_type_step(), other]))
    assert caught.value.path == "$.subgoals[0].condition"


def test_a_value_subgoal_acting_in_two_windows_is_refused() -> None:
    """FV-23: checked in one window only."""
    steps = [
        {
            "id": "o1",
            "subgoal": "g0",
            "verb": "open",
            "target": {"role": "App", "name": "notepad"},
            "risk": "R1",
        },
        {**_type_step("s1"), "window": "opened:o1"},
        {
            "id": "s2",
            "subgoal": "g1",
            "verb": "click",
            "target": {"role": "Button", "name": "OK"},
            "risk": "R0",
            "window": "start",
            "expect": {"title": {"pattern": "x"}},
        },
    ]
    doc = json.loads(_plan_doc(steps))
    doc["subgoals"].insert(0, {"id": "g0", "desc": "d", "condition": {"title": {"pattern": "x"}}})
    with pytest.raises(PlanError) as caught:
        plan_from_json(json.dumps(doc))
    assert caught.value.path == "$.subgoals[1].condition"


# ---- the loop's tick rules (rulings FV-01, FV-20) ---------------------------------------------


def _value_plan(*steps: Any) -> Any:
    from steerai.orchestrator.contracts import Plan, Subgoal  # noqa: PLC0415

    return Plan(
        goal="type hello there",
        subgoals=(
            Subgoal(
                id="g1",
                desc="typed",
                condition=FieldValue(role="Edit", name_pattern="Message", text="hello there"),
            ),
        ),
        steps=steps,
    )


def test_a_value_subgoal_ticks_after_its_type_step() -> None:
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415

    h = Harness()
    result = h.session.run_task(_value_plan(_type()))
    assert result.status == "done", result
    assert result.ticked == {"g1"}


def test_a_value_subgoal_never_ticks_at_a_runs_start() -> None:
    """FV-01/03: a field already holding the text is the type step's business."""
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415

    h = Harness()
    h.world.composer_value = "hello there"
    result = h.session.run_task(_value_plan(_type()))
    assert len(result.results) == 1, "the type step ran: nothing ticked at start"


def test_a_value_subgoal_waits_for_its_last_step() -> None:
    """FV-01: the type held the value; the click after it is part of the subgoal."""
    from dataclasses import replace  # noqa: PLC0415

    from steerai.verify.predicates import TitleMatches  # noqa: PLC0415
    from tests.orchestrator.test_loop import Harness, _click, _type  # noqa: PLC0415

    h = Harness()
    after = replace(_click(step_id="c9", name="Chat options"), expect=TitleMatches(pattern="nope"))
    result = h.session.run_task(_value_plan(_type(), after))
    assert [r.step_id for r in result.results] == ["s2", "c9"], "the click was not skipped"
    assert "g1" not in result.ticked, "the click failed: the subgoal is not done"


def test_the_persons_input_during_a_step_withholds_the_tick() -> None:
    """FV-20: the person may have typed the text."""
    from tests.orchestrator.test_loop import Harness, _type  # noqa: PLC0415
    from tests.orchestrator.test_takeover_loop import FakeMonitor, _wire  # noqa: PLC0415

    h = Harness()
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)

    def person_types(_executor: Any, _action: Any) -> None:
        monitor.user_events += 1  # input counted during the step

    h.semantic.on_execute = person_types
    h.physical.on_execute = person_types
    result = h.session.run_task(_value_plan(_type()))
    assert "g1" not in result.ticked
