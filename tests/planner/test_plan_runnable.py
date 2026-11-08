"""The planner's repair turn also covers refusals the gate makes on every run, and a `window`
that is not a reference is dropped from a plan that opens nothing, where every step acts in the
start window anyway (5.3 [v2.41]; live 2026-11-08 Wikipedia: `$.steps[0].window` refused twice,
a click with no expect blocked `no_predicate`, a submit scoped by its own text)."""

from __future__ import annotations

import json
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


def test_the_start_windows_title_as_window_is_dropped_when_the_plan_opens_nothing() -> None:
    for title in ("Google - Google Chrome", "google"):
        doc = _doc()
        doc.steps[0].window = title
        outcome = ClaudePlanner(parse=Sequence(_Response(parsed_output=doc))).plan(_request())
        assert outcome.repaired is False
        assert "window" not in json.loads(outcome.data)["steps"][0]
        assert outcome.plan.steps[0].window is None


def test_another_windows_title_is_never_dropped() -> None:
    """It may name a window the model meant to act in: refused and repaired, never rewritten
    to the start window (the existing invalid_plan rule)."""
    doc = _doc()
    doc.steps[0].window = "WhatsApp"
    fixed = _doc()
    parse = Sequence(_Response(parsed_output=doc), _Response(parsed_output=fixed))
    outcome = ClaudePlanner(parse=parse).plan(_request())
    assert outcome.repaired is True
    assert "$.steps[0].window" in parse.calls[1]["messages"][2]["content"]


def test_a_valid_reference_is_kept() -> None:
    doc = _doc()
    doc.steps[0].window = "start"
    outcome = ClaudePlanner(parse=Sequence(_Response(parsed_output=doc))).plan(_request())
    assert outcome.plan.steps[0].window == "start"


def test_in_a_continuation_the_start_title_is_never_dropped() -> None:
    """A step without `window` takes the previous step's window, which in a continuation can be
    an opened one: the title stays and is repaired (#137 review item 1)."""
    from dataclasses import replace  # noqa: PLC0415 - this test only

    doc = _doc()
    doc.steps[0].window = "Google - Google Chrome"
    from steerai.planner.contracts import OpenSubgoal  # noqa: PLC0415

    request = replace(_request(), continuation=1, remaining=(OpenSubgoal("g1", "results"),))
    parse = Sequence(_Response(parsed_output=doc), _Response(parsed_output=_doc()))
    outcome = ClaudePlanner(parse=parse).plan(request)
    assert outcome.repaired is True
    assert ".window" in parse.calls[1]["messages"][2]["content"]
