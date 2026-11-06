"""`Step.window` in the contract and the plan file (packet step-window-2026-11-07; schema gate
REVISE 7; hunter SW-18/19/35, OW-17/32): a reference, never a title."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json

OPEN_NOTEPAD = {
    "id": "s1",
    "subgoal": "g1",
    "verb": "open",
    "target": {"role": "App", "name": "notepad"},
    "risk": "R1",
}


def _click(window: Any, step_id: str = "s2") -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": step_id,
        "subgoal": "g1",
        "verb": "click",
        "target": {"role": "Button", "name": "OK"},
        "risk": "R0",
        "expect": {"title": {"pattern": "x"}},
    }
    if window is not ...:
        step["window"] = window
    return step


def _doc(*steps: dict[str, Any]) -> str:
    return json.dumps(
        {
            "goal": "g",
            "subgoals": [{"id": "g1", "desc": "d", "condition": {"title": {"pattern": "x"}}}],
            "steps": list(steps),
        }
    )


@pytest.mark.parametrize("window", ["start", "opened:s1"])
def test_a_reference_round_trips_through_the_file(window: str) -> None:
    plan = plan_from_json(_doc(OPEN_NOTEPAD, _click(window)))
    assert plan.steps[1].window == window
    again = plan_from_json(plan_to_json(plan))
    assert again.steps[1].window == window


def test_a_step_without_window_has_none() -> None:
    assert plan_from_json(_doc(_click(...))).steps[0].window is None


@pytest.mark.parametrize(
    "window",
    [
        "Start",
        " start",
        "opened: s1",
        "opened:",
        "opened:s1 ",
        "",
        None,
        3,
        ["start"],
        "WhatsApp",
        "opened:" + "x" * 65,
        "opened:s\x07",
        "opened:s1\n",
        "start\n",
    ],
)
def test_anything_but_a_reference_is_refused_at_its_path(window: Any) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc(OPEN_NOTEPAD, _click(window)))
    assert caught.value.path == "$.steps[1].window"


def test_an_open_step_takes_no_window() -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc({**OPEN_NOTEPAD, "window": "start"}))
    assert caught.value.path == "$.steps[0].window"


def test_a_reference_to_a_later_or_non_open_step_of_the_same_plan_is_refused() -> None:
    later = plan_from_json  # readability
    with pytest.raises(PlanError) as caught:
        later(_doc(_click("opened:s1", step_id="s0"), OPEN_NOTEPAD))
    assert caught.value.path == "$.steps[0].window"
    with pytest.raises(PlanError) as caught:
        plan_from_json(_doc(_click(..., step_id="s0"), _click("opened:s0", step_id="s2")))
    assert caught.value.path == "$.steps[1].window"


def test_a_reference_to_an_id_outside_the_plan_is_left_to_the_chain() -> None:
    """SW-18: a later link names an earlier link's open; the runner and the ledger check it."""
    plan = plan_from_json(_doc(_click("opened:s1", step_id="p2.s1")))
    assert plan.steps[0].window == "opened:s1"


def test_the_contract_refuses_a_malformed_reference_too() -> None:
    with pytest.raises(ValueError, match="window"):
        Step(
            id="s2",
            subgoal="g1",
            verb="click",
            target=Target(role="Button", name="OK"),
            window="WhatsApp",
        )


def test_a_step_id_at_the_plan_limit_is_a_reference() -> None:
    """SW-35 boundary: 64 characters, the plan's own step id limit (65 is refused above)."""
    ref = "opened:" + "x" * 64
    assert plan_from_json(_doc(_click(ref, step_id="p2.s1"))).steps[0].window == ref


def test_the_contract_refuses_a_window_on_an_open_step() -> None:
    with pytest.raises(ValueError, match="open step takes no window"):
        Step(
            id="s1",
            subgoal="g1",
            verb="open",
            target=Target(role="App", name="notepad"),
            risk="R1",
            window="start",
        )
