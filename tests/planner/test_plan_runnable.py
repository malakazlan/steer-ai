"""The planner's repair turn also covers refusals the gate makes on every run (5.3 [v2.41];
live 2026-11-08 Wikipedia: a click with no expect blocked `no_predicate`, a submit scoped by its
own text). A malformed `window` can no longer be written at all ([v2.42], the enum)."""

from __future__ import annotations

from typing import Any

from steerai.planner.claude_planner import ClaudePlanner
from steerai.planner.mirror import PredicateDoc, StepDoc, TargetDoc
from tests.planner.test_claude_planner import _doc, _request, _Response
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
