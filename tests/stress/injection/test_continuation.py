"""A3 untrusted continuation (IJ-10, IJ-19, IJ-20 pinned): a replan written from a
hostile page cannot change the task. The goal is the request's, set by code whatever the model
writes; the continuation rules refuse a reused ticked subgoal, a rewritten value text, a reused
step id and a second open of the same (app, site). A condition rewritten to a weaker kind (a
title the page controls) must be refused too."""

from __future__ import annotations

from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.replan import Progress, continuation_refusal
from steerai.verify.predicates import Appeared, FieldValue, TitleMatches, WindowExists, Within
from tests.orchestrator.test_replan import (
    GOAL,
    YT,
    _click,
    _open,
    _progress,
    _result,
    _second_plan,
    _unfinished,
    _value_first_plan,
)
from tests.planner.test_claude_planner import _continuation, _doc, _planner, _Response
from tests.stress.injection.catalogue import fixture

# ---- IJ-19: the goal is code's ----------------------------------------------------------------


def test_the_models_goal_is_overwritten_by_the_requests() -> None:
    attack = fixture("IJ-19", "goal")
    doc = _doc()
    doc.goal = attack.payload
    planner, _parse = _planner(_Response(parsed_output=doc))
    request = _continuation()
    outcome = planner.plan(request)
    assert outcome.plan.goal == request.goal
    assert attack.payload not in outcome.data.decode()


def test_a_continuation_with_another_goal_is_refused() -> None:
    attack = fixture("IJ-19", "goal")
    plan = Plan(goal=attack.payload, subgoals=_second_plan().subgoals, steps=(_click("p2.s1"),))
    assert continuation_refusal(plan, _progress()) == attack.detail


# ---- IJ-20: the continuation rules -------------------------------------------------------------


def test_a_reused_ticked_subgoal_is_refused() -> None:
    attack = fixture("IJ-20", "ticked_id")
    plan = _second_plan(
        _click("p2.s1"),
        _click("p2.s2", subgoal="g1"),
        subgoals=(
            Subgoal(id="g1", desc=attack.payload, condition=TitleMatches(pattern="x")),
            Subgoal(id="g2", desc="playing", condition=TitleMatches(pattern="Drake")),
        ),
    )
    assert continuation_refusal(plan, _progress()) == attack.detail


def test_a_value_texts_rewrite_is_refused() -> None:
    attack = fixture("IJ-20", "value_text")
    plan = _second_plan(
        subgoals=(
            Subgoal(
                id="g2",
                desc="hello in notepad",
                condition=FieldValue(
                    role="Document", name_pattern="Text Editor", text=attack.payload
                ),
            ),
        )
    )
    assert continuation_refusal(plan, _progress(_value_first_plan())) == attack.detail


def test_a_reused_step_id_is_refused() -> None:
    attack = fixture("IJ-20", "step_id")
    plan = _second_plan(_click("p2.s1"), _click(attack.payload))
    assert continuation_refusal(plan, _progress()) == attack.detail


def test_a_second_open_of_the_same_pair_is_refused() -> None:
    attack = fixture("IJ-20", "reopen")
    reopen = Step(
        id="p2.o1",
        subgoal="g2",
        verb="open",
        target=Target(role="App", name="chrome"),
        risk="R1",
        params={"site": "youtube_search", "text": attack.payload},
    )
    assert continuation_refusal(_second_plan(reopen), _progress()) == attack.detail


# ---- known gap ---------------------------------------------------------------------------------


def test_a_condition_rewritten_to_a_weaker_kind_is_refused() -> None:
    attack = fixture("IJ-10", "weaker_kind")
    sent = Appeared(role="Text", name_pattern="Message sent", within=Within(landmark="Chat"))
    first = Plan(
        goal=GOAL,
        subgoals=(
            Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT)),
            Subgoal(id="g2", desc="sent", condition=sent),
        ),
        steps=(_open(),),
    )
    progress = Progress()
    progress.add(first, _unfinished(_result("s1")))
    weaker = _second_plan(
        subgoals=(Subgoal(id="g2", desc="sent", condition=TitleMatches(pattern=attack.payload)),)
    )
    assert continuation_refusal(weaker, progress) == attack.detail


def test_a_condition_rewritten_within_its_kind_is_still_allowed() -> None:
    """The kind rule only: a continuation may still rewrite a pattern it reads off the new
    screen (the preview marks it rewritten)."""
    sent = Appeared(role="Text", name_pattern="Message sent", within=Within(landmark="Chat"))
    first = Plan(
        goal=GOAL,
        subgoals=(
            Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT)),
            Subgoal(id="g2", desc="sent", condition=sent),
        ),
        steps=(_open(),),
    )
    progress = Progress()
    progress.add(first, _unfinished(_result("s1")))
    same_kind = Appeared(role="Text", name_pattern="Delivered", within=Within(landmark="Chat"))
    allowed = _second_plan(subgoals=(Subgoal(id="g2", desc="sent", condition=same_kind),))
    assert continuation_refusal(allowed, progress) != "subgoals"


def test_an_open_only_subgoal_may_move_between_window_and_title() -> None:
    """A subgoal its plan reached only by opening an app is judged on the window or its title
    (5.3 [v2.6]); a continuation may swap one for the other."""
    from dataclasses import replace  # noqa: PLC0415 - this test only

    first = Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT)),),
        steps=(_open(),),
    )
    progress = Progress()
    progress.add(first, _unfinished(_result("s1"), ticked=set()))
    retitled = Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g1", desc="results", condition=TitleMatches(pattern=YT)),),
        steps=(replace(_click("p2.s1"), subgoal="g1"),),
    )
    assert continuation_refusal(retitled, progress) != "subgoals"


def test_a_title_placeholder_may_become_a_diff_read_off_the_new_screen() -> None:
    """The recorded YouTube replan (replan3 shape): plan 1 writes a blind title for a subgoal
    with no steps yet; the continuation replaces it with an appeared diff. Stronger, allowed
    (review of #118: 7 of 22 recorded continuations)."""
    first = Plan(
        goal=GOAL,
        subgoals=(
            Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT)),
            Subgoal(id="g2", desc="playing", condition=TitleMatches(pattern="Drake")),
        ),
        steps=(_open(),),
    )
    progress = Progress()
    progress.add(first, _unfinished(_result("s1")))
    diff = Appeared(role="Text", name_pattern="Drake - Rich Flex", within=Within(landmark="Main"))
    stronger = _second_plan(subgoals=(Subgoal(id="g2", desc="playing", condition=diff),))
    assert continuation_refusal(stronger, progress) != "subgoals"
