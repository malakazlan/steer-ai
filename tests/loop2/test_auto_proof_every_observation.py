"""§7.1 ("the verifier tries the code-only rules after every effect"), [V2.29] Q1: the code-only
`navigated` proof judges every observation the loop takes in, not only the one a dispatched
action's watch returns. A result that lands after the watch ended (stage 1's 3d-d: results and
`?q=` 1.5 s after the last keystroke; 3d-a run 3: the page still growing after the type's watch)
is proven on the next observation, with the same baseline, typed and echo guards, before the
controller is asked again. A page that never shows the result never closes."""

from __future__ import annotations

import dataclasses

from steerai.loop2.decision import Decision, SubgoalDoc
from steerai.loop2.runner import run_task
from steerai.world.effects import Observed
from tests.loop2.test_navigated import QUERY_URL, before, page
from tests.loop2.world_fakes import FIELD, RESULT, URL, World, act, report, task

SUBS = [SubgoalDoc(id="s1", desc="results", params={"text": "red shoes"}, closes="page")]
START = dataclasses.replace(before(), focus=FIELD.runtime_id)
RESULTS = dataclasses.replace(page([RESULT]), focus=FIELD.runtime_id)


def _typed_then(*seen: Observed, after: Observed | None = None) -> World:
    """Turn 1 types the query; the type's watch returns `after` (default: the start page, the
    results not yet there); each later `observe` returns the next of `seen`. Every later turn the
    controller would stop blocked, as stage 1's runs did."""
    world = World(
        [
            Decision(subgoals=SUBS, actions=[act("type", target=FIELD.id, param="text")]),
            Decision(stop="blocked"),
            Decision(stop="blocked"),
        ],
        effects=[report("own_value")],
        screen=START,
        observations=[START, START, *seen],  # the first read, turn 1's re-read, then later reads
    )
    world.after = after
    return world


def test_results_that_land_after_the_watch_are_proven_on_the_re_read() -> None:
    """3d-d: the turn-2 decision was made on the read without results and discarded as stale;
    the re-read shows them, and code closes the subgoal before asking again."""
    world = _typed_then(RESULTS)
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert len(world.views) == 2


def test_a_page_still_growing_after_the_watch_is_proven_once_complete() -> None:
    """3d-a run 3: the type's watch read the page part-way (the address holds part of the
    query, no result yet); the complete page arrives on the next read."""
    partial = dataclasses.replace(page([], url=URL + "?q=red"), focus=FIELD.runtime_id)
    world = _typed_then(RESULTS, after=partial)
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert len(world.views) == 2


def test_a_page_that_never_shows_the_result_never_closes() -> None:
    world = _typed_then(START, START)
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"


def test_the_typed_field_alone_is_never_the_result() -> None:
    """The typed guard holds on a late read too: the address carries the words but nothing
    but the field Steer typed into holds them."""
    late = dataclasses.replace(page([], url=QUERY_URL), focus=FIELD.runtime_id)
    world = _typed_then(late, late)
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"


def test_results_already_there_before_the_type_prove_nothing() -> None:
    """The baseline holds on a late read too: a result present at the subgoal's baseline is not
    new, so a later read of the same page closes nothing."""
    shown = dataclasses.replace(page([RESULT], url=URL), focus=FIELD.runtime_id)
    world = _typed_then(RESULTS, RESULTS)
    world.screen = shown
    world.observations = [shown, shown, RESULTS, RESULTS]
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"


def test_the_address_carrier_one_read_later_closes_on_that_read() -> None:
    """[V2.67], the lead's case: the after-type read lists every row but the address has not
    changed yet (no change signal: nothing closes); the next read carries the query and closes."""
    rows_first = dataclasses.replace(page([RESULT], url=URL), focus=FIELD.runtime_id)
    world = _typed_then(RESULTS, after=rows_first)
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert len(world.views) == 2


def test_a_close_at_turn_start_makes_no_model_call_and_is_counted() -> None:
    """[V2.67]: the turn-start judgement closed the task, so that turn's call is not made."""
    world = _typed_then(RESULTS)
    outcome = run_task(task(), world.ports())
    assert outcome.saved_calls == 1
    assert len(world.views) == 2  # turn 1, and the turn-2 call whose read went stale


def test_an_after_read_close_saves_no_call() -> None:
    """Closed by the after-action read inside the batch: no turn-start close, nothing saved."""
    world = _typed_then(after=RESULTS)
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert outcome.saved_calls == 0
    assert len(world.views) == 1


def test_a_poll_read_is_never_judged() -> None:
    poll = dataclasses.replace(RESULTS, tier="poll")
    world = _typed_then(poll, poll)
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"


def test_an_unstable_read_is_never_judged() -> None:
    shaky = dataclasses.replace(
        RESULTS, snapshot=dataclasses.replace(RESULTS.snapshot, stable=False)
    )
    world = _typed_then(shaky, shaky)
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
