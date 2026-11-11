"""The planner's repair turn also covers refusals the gate makes on every run (5.3 [v2.41];
live 2026-11-08 Wikipedia: a click with no expect blocked `no_predicate`, a submit scoped by its
own text). A malformed `window` can no longer be written at all ([v2.42], the enum)."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.planner.claude_planner import ClaudePlanner
from steerai.planner.contracts import PlannerError, PlanRequest
from steerai.planner.mirror import PredicateDoc, StepDoc, TargetDoc
from tests.planner.test_claude_planner import GOAL, _continuation, _doc, _request, _Response
from tests.planner.test_plan_repair import Sequence


def _with_click(expect: PredicateDoc | None) -> Any:
    doc = _doc()
    doc.steps.insert(
        0,
        StepDoc(
            id="open_search",
            subgoal="g1",
            verb="click",
            target=TargetDoc(role="Button", name="Search tools"),
            risk="R0",
            expect=expect,
        ),
    )
    return doc


def test_a_click_without_an_expect_is_repaired() -> None:
    fixed = PredicateDoc(kind="title", pattern="Google")
    parse = Sequence(
        _Response(parsed_output=_with_click(None)), _Response(parsed_output=_with_click(fixed))
    )
    outcome = ClaudePlanner(parse=parse).plan(_request())
    assert outcome.repaired is True
    assert "$.steps[0].expect" in parse.calls[1]["messages"][2]["content"]


def test_a_submit_scoped_by_its_own_text_is_repaired() -> None:
    bad = _doc()
    assert bad.steps[2].params is not None
    bad.steps[2].params.scope = bad.steps[2].params.text
    parse = Sequence(_Response(parsed_output=bad), _Response(parsed_output=_doc()))
    outcome = ClaudePlanner(parse=parse).plan(_request())
    assert outcome.repaired is True
    assert "params.scope" in parse.calls[1]["messages"][2]["content"]


def test_the_click_refusal_tells_the_model_a_best_guess_is_fine() -> None:
    """Live 2026-11-08 (Wikipedia's collapsed search link): the model declined to name what the
    click reveals, on the first call and the repair; a wrong guess only replans (5.3 [v2.42])."""
    parse = Sequence(
        _Response(parsed_output=_with_click(None)),
        _Response(parsed_output=_with_click(PredicateDoc(kind="title", pattern="Google"))),
    )
    ClaudePlanner(parse=parse).plan(_request())
    note = parse.calls[1]["messages"][2]["content"]
    assert "best guess" in note
    assert "the next steps still run" in note


def test_the_prompt_says_a_revealing_click_still_names_its_best_guess() -> None:
    from steerai.planner.claude_planner import build_prompt  # noqa: PLC0415 - this test only

    system, _ = build_prompt(_request(), tag="t")
    prose = " ".join(system.split())
    assert "A click always has an expect" in prose
    assert "best guess" in prose
    assert "replans only if it reaches the end of the plan" in prose


# ---- a continuation's Button submit named twice in the listing (NE-06; rg1's shape) ----------

TWO_SEARCH_BUTTONS = (
    '1  Document "RubyGems"                    [0,0,1100,760]',
    '2    Group "search"                       [84,399,1392,554]',
    '3      ComboBox "Search"                  [84,399,1392,554]',
    '4      Button "Search"                    [1300,399,1392,554]',
    '5    Button "Search"                      [600,600,700,640]',
)


def _button_submit_doc() -> Any:
    doc = _doc()
    doc.steps[2].target = TargetDoc(role="Button", name="Search")
    return doc


def test_a_continuation_button_submit_named_twice_in_the_listing_is_refused() -> None:
    """The plan boundary cannot see the screen, so the planner's own check does: two listed
    Buttons with the submit's exact name would stop `ambiguous_target` at resolve; the repair
    turn names the target instead, and a second refusal is `invalid_plan`."""
    parse = Sequence(
        _Response(parsed_output=_button_submit_doc()),
        _Response(parsed_output=_button_submit_doc()),
    )
    with pytest.raises(PlannerError) as caught:
        ClaudePlanner(parse=parse).plan(_continuation(screen_lines=TWO_SEARCH_BUTTONS))
    assert caught.value.detail == "invalid_plan"
    assert caught.value.path == "$.steps[2].target"
    assert "$.steps[2].target" in parse.calls[1]["messages"][2]["content"]


def test_a_continuation_button_submit_listed_once_is_accepted() -> None:
    once = TWO_SEARCH_BUTTONS[:4]
    parse = Sequence(_Response(parsed_output=_button_submit_doc()))
    outcome = ClaudePlanner(parse=parse).plan(_continuation(screen_lines=once))
    assert outcome.plan.steps[2].target is not None
    assert outcome.plan.steps[2].target.role == "Button"
    assert outcome.repaired is False


def test_a_first_plan_is_not_held_to_the_listing_rule() -> None:
    """Only a continuation goes through a Button; a first plan's Button submit meets the
    resolver (`ambiguous_target`) as before, never the plan-time check."""
    parse = Sequence(_Response(parsed_output=_button_submit_doc()))
    request = PlanRequest(goal=GOAL, screen_lines=TWO_SEARCH_BUTTONS, window_title="RubyGems")
    outcome = ClaudePlanner(parse=parse).plan(request)
    assert outcome.repaired is False
