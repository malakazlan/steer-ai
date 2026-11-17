"""[V2.85] (i), (iii): two counts, wired as `evidence_taken` is. `turns_to_claim`: for each
subgoal closed by a model claim, the turns from the last act under it whose read-back verified
(a type, a choose or a state verb; never a click, a wait or a focus) to the claim's turn, summed
over subgoals. `repeated_words_missing`: subgoals whose claims were refused
`words_missing` more than once (a subgoal counts once, at its second refusal)."""

from __future__ import annotations

from steerai.loop2.decision import ActionDoc, Claim, Decision, SubgoalDoc
from steerai.loop2.runner import run_task
from steerai.world.effects import ValueFact
from tests.loop2.world_fakes import GO, PAGE, World, act, element, observed, task

SAID = ValueFact(filled=True, digest="d:red shoes")
FIELD_BOX = element(42, "Edit", "Shop", (9, 42), 1)
CHECK = element(43, "CheckBox", "Other", (9, 43), 1)
OTHER = element(44, "CheckBox", "Third", (9, 44), 1)


def _value_sub() -> SubgoalDoc:
    return SubgoalDoc(
        id="s1", desc="x", params={"text": "red shoes"}, closes="value", field="unnamed"
    )


def _value_claim() -> Decision:
    return Decision(
        claims=[Claim(subgoal="s1", kind="value", evidence=[FIELD_BOX.id], param="text")]
    )


def _world(turns: list[Decision]) -> World:
    world = World(turns, screen=observed([*PAGE, FIELD_BOX, CHECK, OTHER]))
    world.values[FIELD_BOX.runtime_id] = SAID
    world.fact_text = True
    return world


def _count(*acts_by_turn: list[ActionDoc]) -> int:
    """A value subgoal, one batch of actions per turn, then its claim on the next turn."""
    turns = [Decision(subgoals=[_value_sub()], actions=acts_by_turn[0])]
    turns += [Decision(actions=batch) for batch in acts_by_turn[1:]]
    outcome = run_task(task(), _world([*turns, _value_claim()]).ports())
    assert outcome.status == "done"
    return outcome.turns_to_claim


def _check() -> ActionDoc:
    return act("check", target=CHECK.id, subgoal="s1")


def test_turns_after_the_last_verified_read_back_are_counted() -> None:
    wait = act("wait", subgoal="s1")
    assert _count([_check()], [wait]) == 2
    other = act("check", target=OTHER.id, subgoal="s1")
    assert _count([_check()], [other]) == 1  # the last one, not the first


def test_a_click_a_wait_and_a_focus_are_no_verified_read_back() -> None:
    click = act("click", target=GO.id, subgoal="s1")
    wait = act("wait", subgoal="s1")
    assert _count([click], [wait]) == 0
    assert _count([wait], [wait]) == 0


def test_a_claim_on_the_turn_of_the_verified_act_counts_none() -> None:
    first = Decision(
        subgoals=[_value_sub()],
        actions=[_check()],
        claims=[Claim(subgoal="s1", kind="value", evidence=[FIELD_BOX.id], param="text")],
    )
    outcome = run_task(task(), _world([first]).ports())
    assert outcome.turns_to_claim == 0


def _state_sub() -> SubgoalDoc:
    return SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="state", wanted="on")


def _state_claim() -> Decision:
    return Decision(claims=[Claim(subgoal="s1", kind="state", evidence=[CHECK.id], param="text")])


def _words_missing(claims: int) -> int:
    turns = [Decision(subgoals=[_state_sub()], claims=_state_claim().claims)]
    turns += [_state_claim() for _ in range(claims - 1)]
    world = _world([*turns, Decision(stop="blocked")])
    world.states[(CHECK.runtime_id, "on")] = True
    outcome = run_task(task(), world.ports())
    assert world.views[-1].history.count("claim:s1:words_missing") == claims
    return outcome.repeated_words_missing


def test_a_subgoal_refused_words_missing_more_than_once_is_counted_once() -> None:
    assert _words_missing(2) == 1


def test_one_words_missing_is_not_repeated() -> None:
    assert _words_missing(1) == 0
