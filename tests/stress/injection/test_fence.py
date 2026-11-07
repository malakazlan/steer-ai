"""A1 fence breakout (IJ-11 pinned; IJ-24 and IJ-37 known gaps, #87): screen text in every
fenced slot of the planner's prompt stays data. The goal is the one instruction line at the top,
the closing fence line stands once and last, and a payload stays inside the fence on one line,
even when the attacker knows the fence tag."""

from __future__ import annotations

import pytest

from steerai.planner.claude_planner import build_prompt
from steerai.planner.contracts import DoneStep, OpenSubgoal, PlanRequest
from steerai.world.elements import Element, format_lines, safe_name
from tests.stress.injection.catalogue import FENCE_TAG, Attack, fixture, pinned

GOAL = "send hello there to Amir"
OPENER = f"<<<screen {FENCE_TAG}"
CLOSER = f"{FENCE_TAG}>>>"
DIRECT_SLOTS = 7
"""Window title, app hint, open window, task window, done step role, done step name, open
subgoal."""


def _element(name: str) -> Element:
    return Element(id=4, depth=1, role="Button", name=name, box=(0, 0, 10, 10), runtime_id=(1, 4))


def _request(payload: str) -> PlanRequest:
    """A continuation with the payload in every fenced slot the prompt has."""
    return PlanRequest(
        goal=GOAL,
        screen_lines=format_lines([_element(payload)]),
        window_title=payload,
        app_hint=payload,
        open_windows=(payload,),
        windows=(("start", payload),),
        continuation=1,
        done=(DoneStep(verb="click", role=payload, name=payload),),
        remaining=(OpenSubgoal(id="g1", desc=payload),),
    )


@pytest.mark.parametrize("attack", pinned("IJ-11"), ids=lambda a: a.case)
def test_screen_text_never_leaves_the_fence(attack: Attack) -> None:
    _system, user = build_prompt(_request(attack.payload), tag=FENCE_TAG)
    lines = user.splitlines()
    assert user.split("\n") == lines, "no line boundary of any kind is left inside a line"
    assert lines[:3] == [f"goal: {GOAL}", "", OPENER]
    assert [i for i, line in enumerate(lines) if line.startswith("goal:")] == [0]
    assert [i for i, line in enumerate(lines) if line == CLOSER] == [len(lines) - 1]
    assert lines.count(OPENER) == 1
    inside = lines[3:-1]
    element_line = format_lines([_element(attack.payload)])[0]
    assert inside.count(element_line) == 1, "the listing keeps the escaped name on its own row"
    folded = " ".join(attack.payload.splitlines())
    direct = [line for line in inside if line != element_line]
    assert sum(line.count(folded) for line in direct) == DIRECT_SLOTS


def test_the_system_prompt_names_screen_text_as_data() -> None:
    system, _user = build_prompt(_request(fixture("IJ-11", "ignore").payload), tag=FENCE_TAG)
    assert "never instructions" in system
    assert "Only the goal line is" in " ".join(system.split())


# ---- known gaps (#87, planner-11) ----------------------------------------------------------


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-24 #87")
def test_window_titles_are_escaped_and_capped_like_names() -> None:
    title = fixture("IJ-24", "long_title").payload
    request = PlanRequest(
        goal=GOAL, screen_lines=(), window_title=title, open_windows=(title,), windows=()
    )
    _system, user = build_prompt(request, tag=FENCE_TAG)
    lines = user.splitlines()
    assert f"window: {safe_name(title)}" in lines
    assert f"open window: {safe_name(title)}" in lines


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-37 #87")
def test_a_quote_in_a_name_cannot_forge_a_listing_row() -> None:
    payload = fixture("IJ-37", "quote").payload
    assert '"' not in safe_name(payload)
    assert format_lines([_element(payload)])[0].count('"') == 2
