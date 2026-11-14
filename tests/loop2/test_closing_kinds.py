"""[V2.39]: each subgoal declares at intake the kind of fact that closes it (value, state, page),
frozen with it; a claim or the auto proof closes it only with the matching kind (`kind_mismatch`
otherwise); a page subgoal needs a param. A subgoal closes only when every param is proven on the
same observation. `file:` counts only when the task started on a `file:` page."""

from __future__ import annotations

import dataclasses
from typing import Literal

import pydantic
import pytest

from steerai.loop2 import runner
from steerai.loop2.controller import SYSTEM_RULES
from steerai.loop2.decision import Claim, Decision, SubgoalDoc, validate
from steerai.loop2.runner import run_task
from steerai.loop2.task import GoalSpan, Subgoal
from steerai.loop2.verify import auto_proof, navigated_tokens
from steerai.world.effects import ValueFact
from tests.loop2.test_navigated import TYPED, before, page
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    GOAL,
    MENU,
    PAGE,
    URL,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

# ---- (1) the closing kind ----------------------------------------------------------------------


def test_a_subgoal_without_a_closing_kind_is_not_a_decision() -> None:
    with pytest.raises(pydantic.ValidationError):
        SubgoalDoc(id="s1", desc="search", params={"text": "red shoes"})  # type: ignore[call-arg]  # the point


def test_a_page_subgoal_without_a_param_is_refused_into_the_repair_turn() -> None:
    bare = Decision(subgoals=[SubgoalDoc(id="s1", desc="look", closes="page")], stop="cannot")
    assert "page_without_param" in validate(
        bare, listed_ids=frozenset(), goal=GOAL, first_turn=True
    )


def test_the_prompt_asks_for_the_closing_kind_generically() -> None:
    assert "closes" in SYSTEM_RULES
    assert "value, state or page" in SYSTEM_RULES


def _world(
    closes: str, claim: Claim, *, values: dict[tuple[int, ...], ValueFact] | None = None
) -> World:
    subs = [SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes=closes)]  # type: ignore[arg-type]  # a closed literal
    world = World(
        [
            Decision(subgoals=subs, actions=[act("type", target=FIELD.id, param="text")]),
            Decision(claims=[claim]),
        ],
        effects=[report("own_value")],
    )
    world.values.update(values or {})
    return world


def test_a_value_claim_never_closes_a_page_subgoal() -> None:
    claim = Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")
    world = _world("page", claim)
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert "claim:s1:kind_mismatch" in world.views[-1].history


def test_a_value_claim_closes_a_value_subgoal() -> None:
    claim = Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")
    outcome = run_task(task(), _world("value", claim).ports())
    assert outcome.status == "done"


def test_the_auto_proof_never_closes_a_value_subgoal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def proven(*_a: object, **_k: object) -> str:
        calls.append(1)
        return "title"

    monkeypatch.setattr(runner, "auto_proof", proven)
    moved = dataclasses.replace(
        observed(), url=URL + "?q=red%20shoes", document_url=URL + "?q=red%20shoes"
    )
    subs = [SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="value")]
    world = World(
        [Decision(subgoals=subs, actions=[act("click", target=GO.id)])],
        effects=[report("url_changed_in_place")],
        later=moved,
    )
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert calls == []


# ---- (2) every param on the same observation ---------------------------------------------------

FROM_TO = "fly from Paris to Rome"
A = GoalSpan.of(FROM_TO, FROM_TO.index("Paris"), FROM_TO.index("Paris") + len("Paris"))
B = GoalSpan.of(FROM_TO, FROM_TO.index("Rome"), FROM_TO.index("Rome") + len("Rome"))
TRIP = Subgoal(id="s1", desc="trip", params={"a": A, "b": B}, closes="page")


def test_a_to_b_with_only_a_carried_is_not_proven() -> None:
    """A is in the URL and the title, a content signal by itself; B is nowhere."""
    after = page(url=URL + "?from=Paris", title="Paris - Shop")
    assert auto_proof(TRIP, before(), after, typed=TYPED) is None


def test_a_navigated_claim_with_only_a_carried_is_param_unproven() -> None:
    after = page(url=URL + "?from=Paris", title="Paris - Shop")
    assert navigated_tokens(TRIP, before(), after, cited=[], typed=TYPED) == ("param_unproven",)


def test_a_cited_new_row_holding_b_proves_b_beside_a_carried() -> None:
    row = element(70, "ListItem", "Rome flights", (9, 70), 1)
    after = page([row], url=URL + "?from=Paris", title="Paris - Shop")
    assert navigated_tokens(TRIP, before(), after, cited=[row.id], typed=TYPED) == ()


def test_both_carried_in_the_url_with_one_in_the_title_is_proven() -> None:
    after = page(url=URL + "?from=Paris&to=Rome", title="Paris - Shop")
    assert auto_proof(TRIP, before(), after, typed=TYPED) == "title"


FILL_GOAL = "fill name Ada and city Rome"
NAME = element(40, "Edit", "Name", (9, 40), 1)
CITY = element(41, "Edit", "City", (9, 41), 1)


def _fill_world(name_now: ValueFact, *, gone: bool = False) -> World:
    """Name proven, then a click (the page may change), then city claimed. `gone`: the click's
    page no longer holds the name field."""
    spans = {"name": "Ada", "city": "Rome"}
    subs = [SubgoalDoc(id="s1", desc="fill", params=spans, closes="value")]
    screen = observed([*PAGE, NAME, CITY])
    world = World(
        [
            Decision(
                subgoals=subs,
                claims=[Claim(subgoal="s1", kind="value", evidence=[NAME.id], param="name")],
            ),
            Decision(actions=[act("click", target=MENU.id)]),
            Decision(claims=[Claim(subgoal="s1", kind="value", evidence=[CITY.id], param="city")]),
        ],
        effects=[report("content_changed")],
        screen=screen,
        later=observed([*PAGE, CITY]) if gone else screen,
    )
    world.values[NAME.runtime_id] = ValueFact(filled=True, digest="d:Ada")
    world.values[CITY.runtime_id] = ValueFact(filled=True, digest="d:Rome")
    world.fact_text = True
    world.later_values = {NAME.runtime_id: name_now}
    return world


def _fill_task() -> object:
    return dataclasses.replace(task(), goal=FILL_GOAL)


def test_a_two_field_fill_closes_when_both_read_true_together() -> None:
    world = _fill_world(ValueFact(filled=True, digest="d:Ada"))
    outcome = run_task(_fill_task(), world.ports())  # type: ignore[arg-type]  # a Task
    assert outcome.status == "done"


def test_a_two_field_fill_never_closes_on_a_subset() -> None:
    """The name was proven first, then cleared by the page before the city was proven: the
    closing re-reads both, so it does not close."""
    world = _fill_world(ValueFact(filled=False, digest="d:"))
    outcome = run_task(_fill_task(), world.ports())  # type: ignore[arg-type]  # a Task
    assert outcome.status != "done"


def test_a_two_field_fill_never_closes_when_a_proven_field_is_gone() -> None:
    """The name's element left the page before the city was proven: unread is not true."""
    world = _fill_world(ValueFact(filled=True, digest="d:Ada"), gone=True)
    outcome = run_task(_fill_task(), world.ports())  # type: ignore[arg-type]  # a Task
    assert outcome.status != "done"


def _tick_world(name_later: bool) -> World:
    spans = {"name": "Ada", "city": "Rome"}
    subs = [SubgoalDoc(id="s1", desc="tick", params=spans, closes="state")]
    on: Literal["on"] = "on"
    world = World(
        [
            Decision(
                subgoals=subs,
                claims=[
                    Claim(subgoal="s1", kind="state", evidence=[NAME.id], param="name", wanted=on)
                ],
            ),
            Decision(
                claims=[
                    Claim(subgoal="s1", kind="state", evidence=[CITY.id], param="city", wanted=on)
                ]
            ),
        ],
        screen=observed([*PAGE, NAME, CITY]),
    )
    world.states[(NAME.runtime_id, on)] = True
    world.states[(CITY.runtime_id, on)] = True
    world.later_states = {(NAME.runtime_id, on): name_later}
    return world


def test_two_states_close_when_both_read_true_together() -> None:
    outcome = run_task(_fill_task(), _tick_world(True).ports())  # type: ignore[arg-type]  # a Task
    assert outcome.status == "done"


def test_two_states_never_close_when_the_first_turned_back() -> None:
    outcome = run_task(_fill_task(), _tick_world(False).ports())  # type: ignore[arg-type]  # a Task
    assert outcome.status != "done"


# ---- (3) file: only from a file: start ---------------------------------------------------------

SEARCH = Subgoal(
    id="s1",
    desc="search",
    params={"text": GoalSpan.of(GOAL, GOAL.index("red shoes"), GOAL.index("red shoes") + 9)},
    closes="page",
)


def test_a_file_page_counts_when_the_task_started_on_one() -> None:
    after = page([], url="file:///c:/fixtures/shop.html?q=red%20shoes", title="red shoes")
    assert auto_proof(SEARCH, before(), after, typed=TYPED, allow_file=True) == "title"


def test_a_file_page_is_no_page_when_the_task_started_on_the_web() -> None:
    after = page([], url="file:///c:/fixtures/shop.html?q=red%20shoes", title="red shoes")
    assert auto_proof(SEARCH, before(), after, typed=TYPED, allow_file=False) is None


FILE = "file:///c:/fixtures/shop.html"


def _file_world(start: str) -> World:
    first = dataclasses.replace(before(), url=start, document_url=start)
    return World(
        [decision(act("click", target=GO.id), first=True)],
        effects=[report("navigated")],
        screen=first,
        later=page([], url=FILE + "?q=red%20shoes", title="red shoes"),
    )


def test_a_task_started_on_a_file_page_closes_on_a_file_page() -> None:
    assert run_task(task(), _file_world(FILE).ports()).status == "done"


def test_a_task_started_on_the_web_never_closes_on_a_file_page() -> None:
    assert run_task(task(), _file_world(URL).ports()).status != "done"


# ---- #334 review: one element proves at most one param; a value subgoal needs a param --------


def test_one_element_never_proves_two_state_params() -> None:
    """The principal's repro: params red and shoes, only the "Red" CheckBox on, cited for a and
    then for b. One handle proves at most one param."""
    box = element(42, "CheckBox", "Red", (9, 42), 1)
    on: Literal["on"] = "on"
    subs = [SubgoalDoc(id="s1", desc="tick", params={"a": "red", "b": "shoes"}, closes="state")]
    world = World(
        [
            Decision(
                subgoals=subs,
                claims=[Claim(subgoal="s1", kind="state", evidence=[box.id], param="a", wanted=on)],
            ),
            Decision(
                claims=[Claim(subgoal="s1", kind="state", evidence=[box.id], param="b", wanted=on)]
            ),
        ],
        screen=observed([*PAGE, box]),
    )
    world.states[(box.runtime_id, on)] = True
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert "claim:s1:evidence_reused" in world.views[-1].history
