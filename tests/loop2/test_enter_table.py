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
from steerai.loop2.runner import enter_outcome, run_task, stale_observation
from steerai.world.effects import Observed
from steerai.world.elements import Element
from steerai.world.landmarks import Landmark
from steerai.world.reads import ReadCost
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
    QUERY_PAGE,
    TYPED_VALUE,
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


def _moved(url: str) -> Observed:
    return dataclasses.replace(observed(), url=url, document_url=url)


def test_a_decision_on_an_observation_whose_url_changed_is_discarded_and_re_asked() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), Decision(stop="cannot")],
        observations=[observed(), _moved(URL + "next")],
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
        observations=[observed(), _moved(URL + "a"), _moved(URL + "b")],
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


# ---- #335 review: the staleness rule's unread facts ---------------------------------------------


def _blind(seen: Observed) -> Observed:
    return dataclasses.replace(seen, url=None)


def _bare(seen: Observed) -> Observed:
    """No top Document read (no elements)."""
    return dataclasses.replace(seen, snapshot=dataclasses.replace(seen.snapshot, elements=[]))


def test_a_fact_unread_on_both_sides_is_no_change() -> None:
    seen = observed()
    assert not stale_observation(_blind(seen), _blind(seen))
    assert not stale_observation(_bare(seen), _bare(seen))


def test_a_url_read_on_one_side_only_is_stale_where_the_bar_is_observed() -> None:
    seen = observed()
    assert stale_observation(seen, _blind(seen))
    assert stale_observation(_blind(seen), seen)


def test_a_url_absent_on_one_side_without_an_address_bar_is_no_change() -> None:
    """A window without an address bar never observes the in-place URL: its absence is not
    unread (`url_observed`), as for the observer's `url` fact."""
    seen = observed()
    native = dataclasses.replace(_blind(seen), url_observed=False)
    assert not stale_observation(seen, native)
    assert not stale_observation(native, seen)


def test_a_top_document_on_one_side_only_is_stale() -> None:
    seen = observed()
    assert stale_observation(seen, _bare(seen))
    assert stale_observation(_bare(seen), seen)


def test_a_url_that_turns_unread_after_the_decision_is_never_dispatched_on() -> None:
    """The principal's repro: the Decision read the URL; the fresh read has none, same
    Document: re-asked, nothing dispatched."""
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        observations=[observed(), _blind(observed())],
    )
    run_task(task(), world.ports())
    assert world.dispatched == []
    assert "stale_observation" in world.views[1].history


# ---- #335 review: row 2 on a re-rendered pending field ------------------------------------------

REBUILT = element(77, "Edit", "Search", (9, 77), 1)
"""FIELD re-rendered: the same ledger identity (role, name, automation id), a new RuntimeId."""


TWIN = element(78, "Edit", "Search", (9, 78), 1)


def _rebuilt(*shown: Element) -> Observed:
    rest = [e for e in QUERY_PAGE if e.id != FIELD.id]
    roles = {e.runtime_id: "searchbox" for e in shown}  # the page keeps the field's role
    return dataclasses.replace(observed([*rest, *shown]), aria_roles=roles)


def _rebuilt_world(*shown: Element, final: Observed | None = None) -> World:
    """Type, the field re-renders as `shown`, then a submit click whose final report shows
    `final` (default: the same page)."""
    world = World(
        [decision(TYPE, first=True), decision(act("click", target=GO.id))],
        # a carrier, so only row 2 (the field emptied) can stop it
        effects=[report("own_value"), report("content_changed", carriers=["title"])],
        screen=FOCUSED,
        later=_rebuilt(*shown),
        timeline=[] if final is None else [(2, final)],
        cleared={e.runtime_id: 2 for e in shown},
    )
    for e in {*shown, TWIN}:
        world.values[e.runtime_id] = TYPED_VALUE
    return world


def test_a_re_rendered_pending_field_emptied_by_a_submit_click_stops() -> None:
    """The principal's repro: type, the field re-renders with the same identity, a submit click,
    the field empties: row 2 stops (it read `not_typed` and went to row 6)."""
    world = _rebuilt_world(REBUILT)
    outcome = run_task(task(), world.ports())
    assert [(d.action.verb, d.submit) for d in world.dispatched] == [
        ("type", False),
        ("click", True),
    ]
    assert (outcome.status, outcome.reason) == ("stopped", "unexpected_effect")


def test_a_pending_field_ambiguous_before_the_click_is_never_clicked_through() -> None:
    """Two elements carry the field's identity before the click: the pending text is unread,
    and the risk path fails closed (R2), so the click is never sent in step 3."""
    world = _rebuilt_world(REBUILT, TWIN)
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type"]
    assert outcome.reason == "commit_not_in_step"


def test_a_pending_field_ambiguous_on_the_final_report_is_unread_on_row_2() -> None:
    """One match before the click, two on the final report: which one holds the text is not
    known, so row 2 reads `unread` and gives `unknown`, never a guess."""
    world = _rebuilt_world(REBUILT, final=_rebuilt(REBUILT, TWIN))
    run_task(task(), world.ports())
    assert "enter:unknown:2" in _history(world)


# ---- #335 review: the submit hold is checked --------------------------------------------------


def test_a_submit_report_neither_held_nor_timed_out_is_row_5() -> None:
    """No hold facts (a watch that did not hold the submit): the report is not final, so a row
    that is not a stop is `unknown` (row 5), never `expected`."""
    world = World(
        [decision(TYPE, ENTER, first=True)],
        effects=[report("own_value"), report("navigated", carriers=["url_param"])],
        screen=FOCUSED,
        unheld=True,
    )
    run_task(task(), world.ports())
    assert "enter:unknown:5" in _history(world)


def test_an_unheld_submit_report_that_is_a_stop_still_stops() -> None:
    world = World(
        [decision(TYPE, ENTER, first=True)],
        effects=[report("own_value"), report("navigated", tokens=["windows_changed"])],
        screen=FOCUSED,
        unheld=True,
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "unexpected_effect")


# ---- #335 review: the post-decision observe is a read the task paid for ([V2.34]) -------------


def test_the_post_decision_observe_is_logged_in_read_costs() -> None:
    cost = ReadCost(full_reads=1, full_ms=40)
    fresh = dataclasses.replace(observed(), read_cost=cost)
    world = World(
        [decision(act("click", target=GO.id), first=True)], observations=[observed(), fresh]
    )
    outcome = run_task(task(), world.ports())
    assert cost in outcome.read_costs


# ---- the tester's #335 kill tests ---------------------------------------------------------------


def test_the_outcome_counts_the_stale_observations() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), Decision(stop="cannot")],
        observations=[observed(), _moved(URL + "next")],
    )
    assert run_task(task(), world.ports()).stale_observations == 1


def test_a_fresh_turn_resets_the_stale_streak() -> None:
    """Stale, fresh (dispatched), stale: two stale turns, not in a row: no strike."""
    a, b = _moved(URL + "a"), _moved(URL + "b")
    world = World(
        [decision(act("click", target=GO.id), first=True)] * 3,
        observations=[observed(), a, a, b],
        effects=[report("own_state")],
    )
    outcome = run_task(task(), world.ports())
    assert len(world.dispatched) == 1
    assert outcome.stale_observations == 2
    assert outcome.strikes == ()


def test_the_batch_resolves_on_the_decided_listing_so_a_renamed_element_is_refused() -> None:
    """RT-17: the ids are the listing the Decision was made on; the fresh read renamed the
    target, so the role-and-name re-check refuses it."""
    renamed = element(GO.id, "Button", "Find", GO.runtime_id, 1)
    fresh = observed([renamed if e.id == GO.id else e for e in PAGE])
    world = World(
        [decision(act("click", target=GO.id), first=True)], observations=[observed(), fresh]
    )
    run_task(task(), world.ports())
    assert world.dispatched == []


def test_a_results_carrier_without_a_url_carrier_carries_nothing() -> None:
    outcome = enter_outcome(
        report("content_changed", carriers=["results"]),
        before=observed(),
        after=observed(),
        typed_field="kept",
    )
    assert (outcome.outcome, outcome.row) == ("unexpected_effect", "4")


def test_a_rerender_of_the_same_top_document_is_not_row_3() -> None:
    same = enter_outcome(
        report("content_changed", tokens=["rerendered"], carriers=["title"]),
        before=observed(),
        after=observed(),
        typed_field="kept",
    )
    assert same.row != "3"
    replaced = dataclasses.replace(PAGE[0], runtime_id=(9, 501))
    other = enter_outcome(
        report("content_changed", tokens=["rerendered"], carriers=["title"]),
        before=observed(),
        after=observed([replaced, *PAGE[1:]]),
        typed_field="kept",
    )
    assert (other.outcome, other.row) == ("unexpected_effect", "3")


def test_the_worst_pending_field_wins_emptied_over_kept() -> None:
    second = element(50, "Edit", "Find", (9, 50), 1)
    shown = [*QUERY_PAGE, second]
    screen = dataclasses.replace(
        observed(shown, focus=FIELD.runtime_id),
        aria_roles={FIELD.runtime_id: "searchbox", second.runtime_id: "searchbox"},
    )
    world = World(
        [
            decision(
                act("type", target=FIELD.id, param="text"),
                act("type", target=second.id, param="text"),
                act("click", target=GO.id),
                first=True,
            )
        ],
        effects=[
            report("own_value"),
            report("own_value"),
            report("content_changed", carriers=["title"]),
        ],
        screen=screen,
        cleared={FIELD.runtime_id: 3},
    )
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "type", "click"]
    assert (outcome.status, outcome.reason) == ("stopped", "unexpected_effect")


def test_a_pending_field_unmatched_on_a_partial_read_is_unread_on_row_2() -> None:
    """Two fields typed; the page is still growing and the first one is not in it (gone is not
    proven on a partial read, so it stays pending, unmatched). A submit for the second: the first
    is `unread`, never `gone`, so row 2 gives `unknown`."""
    second = element(50, "Edit", "Find", (9, 50), 1)
    roles = {FIELD.runtime_id: "searchbox", second.runtime_id: "searchbox"}
    screen = dataclasses.replace(observed([*QUERY_PAGE, second]), aria_roles=roles)
    partial = dataclasses.replace(
        observed([e for e in QUERY_PAGE if e.id != FIELD.id] + [second]),
        aria_roles=roles,
        growing=True,
    )
    world = World(
        [
            decision(
                act("type", target=FIELD.id, param="text"),
                act("type", target=second.id, param="text"),
                first=True,
            ),
            decision(act("click", target=GO.id)),
        ],
        effects=[
            report("own_value"),
            report("own_value"),
            report("content_changed", carriers=["title"]),
        ],
        screen=screen,
        later=partial,
        later_after_acts=2,
    )
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "type", "click"]
    assert "enter:unknown:2" in _history(world)
