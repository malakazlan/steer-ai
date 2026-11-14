"""PR C ([V2.28] item 1, [V2.21]-[V2.24], [V2.34] #308): the Enter table evaluated once, on the
watch's final report of a submit (the watch holds a submit to a conclusive report, #310), and the
staleness rule: a Decision made on an observation whose top Document was replaced or whose URL
changed is discarded, re-observed and re-asked, with no strike, and two in a row are a strike."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.loop2 import runner
from steerai.loop2._stub import PENDING
from steerai.loop2.decision import Decision
from steerai.loop2.query import query_outcome
from steerai.loop2.runner import run_task
from steerai.world.landmarks import Landmark
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
    QUERY_PAGE,
    URL,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

TYPE = act("type", target=FIELD.id, param="text")
ENTER = act("press", key="Enter")
FOCUSED = observed(QUERY_PAGE, focus=FIELD.runtime_id)


def _history(world: World) -> list[str]:
    return [t for view in world.views for t in view.history]


# ---- the Enter table on the final report -------------------------------------------------------


def test_308_a_search_or_add_field_in_a_search_landmark_then_emptied_stops() -> None:
    """[V2.34] #308, a recorded limit: the query class trusts the page's search semantics. The
    Enter is dispatched at R1; the field then empties (row 2), so the task stops."""
    form = element(60, "Group", "", (9, 60), 1)
    box = element(61, "Edit", "Search or add a task", (9, 61), 60)
    shown = [*PAGE, form, box]
    search = Landmark(kind="search", runtime_id=form.runtime_id)
    screen = dataclasses.replace(
        observed(shown, focus=box.runtime_id, searchbox=False),
        landmarks={e.runtime_id: None for e in shown} | {box.runtime_id: search},
    )
    world = World(
        [decision(act("type", target=box.id, param="text"), ENTER, first=True)],
        # a carrier, so only row 2 (the field emptied) can stop it; without it row 4 would too
        effects=[report("own_value"), report("content_changed", carriers=["title"])],
        screen=screen,
        cleared={box.runtime_id: 2},
    )
    outcome = run_task(task(), world.ports())
    assert [(d.action.verb, d.risk) for d in world.dispatched] == [("type", "R0"), ("press", "R1")]
    assert (outcome.status, outcome.reason) == ("stopped", "unexpected_effect")


def test_an_enter_on_a_typed_field_is_watched_with_the_hold() -> None:
    world = World(
        [decision(TYPE, ENTER, first=True)],
        effects=[report("own_value"), report("none")],
        screen=FOCUSED,
    )
    run_task(task(), world.ports())
    assert [(d.action.verb, d.submit) for d in world.dispatched] == [
        ("type", False),
        ("press", True),
    ]


def test_a_plain_click_with_nothing_typed_is_watched_without_the_hold() -> None:
    world = World([decision(act("click", target=GO.id), first=True)], effects=[report("own_state")])
    run_task(task(), world.ports())
    assert [(d.action.verb, d.submit) for d in world.dispatched] == [("click", False)]


def test_a_click_that_submits_pending_text_is_watched_with_the_hold() -> None:
    world = World(
        [decision(TYPE, first=True), decision(act("click", target=GO.id))],
        effects=[report("own_value"), report("none")],
        screen=FOCUSED,
    )
    run_task(task(), world.ports())
    assert [(d.action.verb, d.submit) for d in world.dispatched] == [
        ("type", False),
        ("click", True),
    ]


def test_the_table_is_evaluated_once_per_submit_on_the_final_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []
    real = query_outcome

    def spy(report: object, **kw: object) -> object:
        seen.append(report)
        return real(report, **kw)  # type: ignore[arg-type]  # passed through unchanged

    monkeypatch.setattr(runner, "query_outcome", spy)
    final = report("none")
    world = World(
        [decision(TYPE, ENTER, first=True)], effects=[report("own_value"), final], screen=FOCUSED
    )
    run_task(task(), world.ports())
    assert seen == [final]


def test_a_none_after_the_enter_goes_back_to_the_controller_with_row_9() -> None:
    world = World(
        [decision(TYPE, ENTER, first=True)],
        effects=[report("own_value"), report("none")],
        screen=FOCUSED,
    )
    outcome = run_task(task(), world.ports())
    assert "enter:none:9" in _history(world)
    assert outcome.reason != "unexpected_effect"


def test_a_carried_navigation_is_expected_row_6() -> None:
    effects = [report("own_value"), report("navigated", carriers=["url_param"])]
    world = World([decision(TYPE, ENTER, first=True)], effects=effects, screen=FOCUSED)
    run_task(task(), world.ports())
    assert "enter:none:6" in _history(world)


def test_a_window_change_after_the_enter_stops_row_1() -> None:
    effects = [report("own_value"), report("navigated", tokens=["windows_changed"])]
    world = World([decision(TYPE, ENTER, first=True)], effects=effects, screen=FOCUSED)
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "unexpected_effect")


def test_a_plain_click_never_goes_through_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(runner, "query_outcome", lambda *_a, **_k: calls.append(1))
    world = World([decision(act("click", target=GO.id), first=True)], effects=[report("own_state")])
    run_task(task(), world.ports())
    assert calls == []


# ---- the staleness rule ------------------------------------------------------------------------


def _moved(url: str) -> object:
    return dataclasses.replace(observed(), url=url, document_url=url)


def test_a_decision_on_an_observation_whose_url_changed_is_discarded_and_re_asked() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), Decision(stop="cannot")],
        observations=[observed(), _moved(URL + "next")],  # type: ignore[list-item]  # an Observed
    )
    outcome = run_task(task(), world.ports())
    assert world.dispatched == []
    assert "stale_observation" in world.views[1].history
    assert outcome.strikes == ()


def test_a_replaced_top_document_is_stale_too() -> None:
    new_doc = dataclasses.replace(PAGE[0], runtime_id=(9, 501))
    replaced = observed([new_doc, *PAGE[1:]])
    world = World(
        [decision(act("click", target=GO.id), first=True), Decision(stop="cannot")],
        observations=[observed(), replaced],
    )
    run_task(task(), world.ports())
    assert world.dispatched == []
    assert "stale_observation" in world.views[1].history


def test_two_stale_turns_in_a_row_are_a_strike() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("click", target=GO.id))],
        observations=[observed(), _moved(URL + "a"), _moved(URL + "b")],  # type: ignore[list-item]  # Observed
    )
    outcome = run_task(task(), world.ports())
    assert world.dispatched == []
    assert len(outcome.strikes) == 1


def test_a_title_change_alone_is_not_stale() -> None:
    """Only the top Document and the URL are compared: a ticker page never looks stale."""
    ticked = dataclasses.replace(
        observed(), snapshot=dataclasses.replace(observed().snapshot, title="Shop (3)")
    )
    world = World(
        [decision(act("click", target=GO.id), first=True)], observations=[observed(), ticked]
    )
    run_task(task(), world.ports())
    assert [d.runtime_id for d in world.dispatched] == [GO.runtime_id]


def test_pr_c_stubs_are_built() -> None:
    assert (
        not {"steerai.loop2.runner.stale_observation", "steerai.loop2.runner.enter_outcome"}
        & PENDING
    )
