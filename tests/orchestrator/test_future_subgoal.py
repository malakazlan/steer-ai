"""A subgoal with no steps of its own, listed after a subgoal that has steps, describes a state
the plan has not reached yet: it never ticks at task start (5.11 [v2.40]). Live 2026-11-08 (task
suite, YouTube "search ... and play the first video", 2 of 5 runs): the planner wrote g2 "the
first video is playing" as `title "YouTube"` with no steps; it held at start, so the task ended
`done` on the results page with no video opened. A step-less precondition listed first still
ticks; a later step-less subgoal is left to a continuation, which plans its steps."""

from __future__ import annotations

from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.verify.predicates import TitleMatches
from tests.orchestrator.test_loop import Harness

CHAT = TitleMatches(pattern="Amir Khan - Chat")  # holds on the fake page from the start


def _click() -> Step:
    return Step(
        id="s1",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="Chat options"),
        risk="R0",
        expect=TitleMatches(pattern="Options open"),
    )


def test_a_later_step_less_subgoal_never_ticks_at_start() -> None:
    h = Harness()
    plan = Plan(
        goal="open chat options and see the video play",
        subgoals=(
            Subgoal(id="g1", desc="options open", condition=TitleMatches(pattern="Options open")),
            Subgoal(id="g2", desc="the video is playing", condition=CHAT),
        ),
        steps=(_click(),),
    )
    result = h.session.run_task(plan)
    assert "g2" not in result.ticked
    assert result.status == "stopped", "unfinished: a continuation plans g2's steps"


def test_a_step_less_precondition_listed_first_still_ticks() -> None:
    h = Harness()
    plan = Plan(
        goal="in the chat, open chat options",
        subgoals=(
            Subgoal(id="g0", desc="the chat is open", condition=CHAT),
            Subgoal(id="g1", desc="options open", condition=TitleMatches(pattern="Options open")),
        ),
        steps=(_click(),),
    )
    result = h.session.run_task(plan)
    assert "g0" in result.ticked


def test_a_plan_whose_only_subgoal_holds_needs_no_step() -> None:
    """PS-45: an end state already reached is done with nothing to run (unchanged)."""
    h = Harness()
    plan = Plan(
        goal="open the chat",
        subgoals=(Subgoal(id="g0", desc="chat open", condition=CHAT),),
        steps=(
            Step(
                id="o1",
                subgoal="g0",
                verb="click",
                target=Target(role="ListItem", name="Amir · hi bro · 2h"),
                risk="R0",
                expect=CHAT,
            ),
        ),
    )
    assert h.session.run_task(plan).status == "done"
