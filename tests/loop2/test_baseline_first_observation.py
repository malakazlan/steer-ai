"""[V2.72] (RT-39 tightened): every subgoal's baseline is the task's first full observation and
never moves later; which subgoal an action was dispatched under plays no part (the model assigns
those labels freely). When a subgoal closes, any other open subgoal of the same closing kind with
a shared param span takes that observation as its baseline, so one event never closes two
subgoals of the same kind. A value and a page subgoal sharing a span may both close on one type
(a field's digest and a page's carriers are different evidence)."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.runner import _Run, run_task, spans_shared
from steerai.loop2.task import GoalSpan, Subgoal
from steerai.world.effects import Observed
from tests.loop2.test_auto_proof_every_observation import RESULTS, START
from tests.loop2.test_navigated import QUERY_URL, page
from tests.loop2.world_fakes import FIELD, GO, GOAL, RESULT, URL, World, act, report, task

PAGE_SUB = SubgoalDoc(id="s1", desc="results", params={"text": "red shoes"}, closes="page")
VALUE_SUB = SubgoalDoc(id="s2", desc="field", params={"text": "red shoes"}, closes="value")
MOVING = dataclasses.replace(RESULTS, snapshot=dataclasses.replace(RESULTS.snapshot, stable=False))
"""The results already there, on a read still unstable (never judged)."""
VALUE_CLAIM = Claim(subgoal="s2", kind="value", evidence=[FIELD.id], param="text")


def _typed_under_value(*later: Decision, after: Observed | None) -> World:
    """3d-a run 1's shape: a page and a value subgoal over one span; the type goes under the
    value subgoal, so the page subgoal never has an action of its own."""
    typed = act("type", target=FIELD.id, param="text", subgoal="s2")
    world = World(
        [Decision(subgoals=[PAGE_SUB, VALUE_SUB], actions=[typed]), *later],
        effects=[report("own_value")],
        screen=START,
    )
    world.after = after
    if after is not None:
        world.timeline = [(1, after)]  # the page stays as the type's watch left it
    return world


def _proven(world: World, sid: str) -> bool:
    return any(line.startswith(f"proven:{sid}:") for v in world.views for line in v.history)


def test_the_traced_shape_closes_done() -> None:
    """3d-a run 1: both closing actions under the value subgoal, the address changes after the
    type; the page subgoal closes on the code-only proof, the value one on its claim."""
    world = _typed_under_value(Decision(claims=[VALUE_CLAIM]), after=RESULTS)
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert _proven(world, "s1")


def test_words_already_in_the_address_at_the_start_never_close_on_that_alone() -> None:
    shown = dataclasses.replace(page([RESULT], url=QUERY_URL), focus=FIELD.runtime_id)
    world = _typed_under_value(
        Decision(claims=[VALUE_CLAIM]), Decision(stop="blocked"), after=shown
    )
    world.screen = shown
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert not _proven(world, "s1")


def test_the_baseline_does_not_move_when_a_later_action_goes_under_the_page_subgoal() -> None:
    """The results land while every read is still unstable (never judged); a later wait goes
    under the page subgoal. Moving the baseline to just before that wait would put it after the
    address already carries the words, and the page subgoal could never close."""
    world = _typed_under_value(
        Decision(actions=[act("wait", subgoal="s1")]),
        Decision(claims=[VALUE_CLAIM]),
        after=None,
    )
    world.timeline = [(1, MOVING), (2, RESULTS)]
    outcome = run_task(task(), world.ports())
    assert _proven(world, "s1")
    assert outcome.status == "done"


def test_one_event_never_closes_two_page_subgoals_over_one_span() -> None:
    """ "search X, then open the X page": the second page subgoal does not close on the evidence
    that closed the first; its baseline moves to that observation."""
    search = SubgoalDoc(id="s1", desc="search", params={"text": "red shoes"}, closes="page")
    open_page = SubgoalDoc(id="s2", desc="open", params={"name": "red shoes"}, closes="page")
    world = World(
        [
            Decision(
                subgoals=[search, open_page],
                actions=[act("type", target=FIELD.id, param="text", subgoal="s1")],
            ),
            Decision(stop="blocked"),
        ],
        effects=[report("own_value")],
        screen=START,
    )
    world.after = RESULTS
    outcome = run_task(task(), world.ports())
    assert _proven(world, "s1")
    assert not _proven(world, "s2")
    assert outcome.status != "done"


def test_a_value_and_a_page_subgoal_over_one_span_both_close_on_one_type() -> None:
    """Different kinds are different evidence: the value subgoal closes first, on its claim,
    while the read is still unstable; that close must not move the page subgoal's baseline, so
    the page closes on the same type's results once a stable read shows them."""
    world = _typed_under_value(
        Decision(claims=[VALUE_CLAIM]),
        Decision(actions=[act("wait", subgoal="s1")]),
        Decision(stop="blocked"),
        after=None,
    )
    world.timeline = [(1, MOVING), (2, RESULTS)]
    outcome = run_task(task(), world.ports())
    assert _proven(world, "s2")  # the value closed first, on turn 2
    assert outcome.status == "done"  # and the page on the same type's results


def test_the_second_page_subgoal_closes_on_a_later_event_of_its_own() -> None:
    """#473 review 2, newer and not never: after the first page subgoal closed on the results,
    a later navigation that carries the second's words (a title it did not have) closes it."""
    search = SubgoalDoc(id="s1", desc="search", params={"text": "red shoes"}, closes="page")
    open_page = SubgoalDoc(id="s2", desc="open", params={"name": "red shoes"}, closes="page")
    item = page([], url=URL + "item/42", title="Red shoes")
    world = World(
        [
            Decision(
                subgoals=[search, open_page],
                actions=[act("click", target=GO.id, subgoal="s1")],
            ),
            Decision(actions=[act("click", target=RESULT.id, subgoal="s2")]),
            Decision(stop="blocked"),
        ],
        effects=[report("navigated"), report("navigated")],
        screen=START,
    )
    world.timeline = [(1, page([RESULT])), (2, item)]
    outcome = run_task(task(), world.ports())
    assert _proven(world, "s1")
    assert outcome.status == "done"


# ---- which params share a span ([V2.72] Two, #473 review 1) -----------------------------------


def _span(text: str) -> GoalSpan:
    start = GOAL.index(text)
    return GoalSpan(text, start, start + len(text))


@pytest.mark.parametrize(
    ("a", "b", "shared"),
    [
        (_span("red shoes"), _span("red shoes"), True),
        (_span("red shoes"), _span("shoes"), True),  # contained
        (_span("the shop for red"), _span("red shoes"), True),  # overlapping
        (_span("red shoes"), _span("shop"), False),  # disjoint
        (_span("red shoes"), GoalSpan("Red  Shoes", 40, 50), True),  # the same words elsewhere
    ],
    ids=["same", "contained", "overlapping", "disjoint", "same_words_elsewhere"],
)
def test_two_params_share_a_span_when_they_overlap_or_say_the_same_words(
    a: GoalSpan, b: GoalSpan, shared: bool
) -> None:
    assert spans_shared(a, b) is shared
    assert spans_shared(b, a) is shared


def _sub(sid: str, text: str, closes: str = "page") -> Subgoal:
    return Subgoal(id=sid, desc="x", params={"p": _span(text)}, closes=closes)  # type: ignore[arg-type]  # a closing kind under test


def test_a_close_moves_only_open_same_kind_subgoals_that_share_a_span() -> None:
    closed = _sub("s1", "red shoes")
    run = _Run(task(), World([]).ports())
    run.subgoals = (
        closed,
        _sub("s2", "shoes"),  # contained: moved
        _sub("s3", "shop"),  # disjoint: kept
        _sub("s4", "red shoes", "value"),  # another kind: kept
    )
    run.proven.add("s1")
    run._rebase(closed, START)
    assert run.baselines == {"s2": START}
