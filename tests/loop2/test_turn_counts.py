"""[V2.85] (i), (iii): two counts, wired as `evidence_taken` is. `turns_to_claim`: for each
subgoal closed by a model claim after a delivered_verified action under it (a `wait` actuates
nothing and does not count), the turns from the first such action's turn to the claim's turn,
summed over subgoals. `repeated_words_missing`: subgoals whose claims were refused
`words_missing` more than once (a subgoal counts once, at its second refusal)."""

from __future__ import annotations

from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.runner import run_task
from steerai.world.effects import ValueFact
from tests.loop2.world_fakes import GO, PAGE, World, act, element, observed, task

SAID = ValueFact(filled=True, digest="d:red shoes")
FIELD_BOX = element(42, "Edit", "Shop", (9, 42), 1)
CHECK = element(43, "CheckBox", "Other", (9, 43), 1)


def _value_sub() -> SubgoalDoc:
    return SubgoalDoc(
        id="s1", desc="x", params={"text": "red shoes"}, closes="value", field="unnamed"
    )


def _value_claim() -> Decision:
    return Decision(
        claims=[Claim(subgoal="s1", kind="value", evidence=[FIELD_BOX.id], param="text")]
    )


def _world(turns: list[Decision]) -> World:
    world = World(turns, screen=observed([*PAGE, FIELD_BOX, CHECK]))
    world.values[FIELD_BOX.runtime_id] = SAID
    world.fact_text = True
    return world


def test_turns_between_a_verified_action_and_its_claim_are_counted() -> None:
    world = _world(
        [
            Decision(subgoals=[_value_sub()], actions=[act("click", target=GO.id, subgoal="s1")]),
            Decision(actions=[act("wait", subgoal="s1")]),
            _value_claim(),
        ]
    )
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert outcome.turns_to_claim == 2


def test_a_claim_on_the_turn_of_the_verified_action_counts_none() -> None:
    first = Decision(
        subgoals=[_value_sub()],
        actions=[act("click", target=GO.id, subgoal="s1")],
        claims=[Claim(subgoal="s1", kind="value", evidence=[FIELD_BOX.id], param="text")],
    )
    outcome = run_task(task(), _world([first]).ports())
    assert outcome.turns_to_claim == 0


def test_a_wait_is_no_delivered_action_and_a_subgoal_with_none_counts_none() -> None:
    world = _world(
        [
            Decision(subgoals=[_value_sub()], actions=[act("wait", subgoal="s1")]),
            Decision(actions=[act("wait", subgoal="s1")]),
            _value_claim(),
        ]
    )
    assert run_task(task(), world.ports()).turns_to_claim == 0


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
