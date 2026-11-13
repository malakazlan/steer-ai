from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from steerai.loop2.decision import ActionDoc, Claim, Decision, SubgoalDoc, validate

GOAL = "write hello world to the note"
LISTED = frozenset({1, 2, 3})
S1_PARAMS = {"s1": frozenset({"text"})}


def check(
    decision: Decision,
    *,
    first_turn: bool = False,
    subgoal_params: Mapping[str, frozenset[str]] | None = None,
) -> list[str]:
    return validate(
        decision,
        listed_ids=LISTED,
        goal=GOAL,
        first_turn=first_turn,
        subgoal_params=subgoal_params,
    )


def action(verb: str, **kw: object) -> ActionDoc:
    base: dict[str, object] = {"verb": verb, "subgoal": "s1"}
    base.update(kw)
    return ActionDoc.model_validate(base)


def act(verb: str, **kw: object) -> Decision:
    return Decision(actions=[action(verb, **kw)])


def first(params: dict[str, str]) -> Decision:
    return Decision(
        subgoals=[SubgoalDoc(id="s1", desc="d", params=params)],
        actions=[action("click", target=1)],
    )


def test_clean_decision_has_no_tokens() -> None:
    assert check(act("click", target=1)) == []


def test_id_not_listed_for_action() -> None:
    assert check(act("click", target=99)) == ["id_not_listed"]


def test_id_not_listed_for_evidence() -> None:
    d = Decision(claims=[Claim(subgoal="s1", kind="state", evidence=[99], wanted="on")])
    assert check(d) == ["id_not_listed"]


@pytest.mark.parametrize("verb", ["press", "wait"])
def test_target_on_untargeted_verb(verb: str) -> None:
    assert check(act(verb, target=1, key="Enter")) == ["target_on_untargeted_verb"]


@pytest.mark.parametrize("verb", ["open", "switch"])
def test_open_and_switch_not_in_step(verb: str) -> None:
    assert check(act(verb)) == ["not_in_step"]


def test_target_missing() -> None:
    assert check(act("click")) == ["target_missing"]


def test_key_missing() -> None:
    assert check(act("press")) == ["key_missing"]


def test_press_with_key_ok() -> None:
    assert check(act("press", key="Enter")) == []


def test_param_not_span() -> None:
    assert check(first({"text": "goodbye"}), first_turn=True) == ["param_not_span"]


def test_param_span_ok() -> None:
    assert check(first({"text": "hello world"}), first_turn=True) == []


def test_subgoals_after_first() -> None:
    d = Decision(subgoals=[SubgoalDoc(id="s1", desc="d")], actions=[action("click", target=1)])
    assert check(d, first_turn=False) == ["subgoals_after_first"]


def test_subgoals_missing() -> None:
    assert "subgoals_missing" in check(act("click", target=1), first_turn=True)


@pytest.mark.parametrize("kind", ["content_present", "content_gone", "window", "file", "answer"])
def test_not_in_step_claims(kind: str) -> None:
    d = Decision(claims=[Claim.model_validate({"subgoal": "s1", "kind": kind})])
    assert check(d) == ["not_in_step"]


def test_option_missing() -> None:
    assert check(act("choose", target=1)) == ["option_missing"]


def test_text_missing() -> None:
    assert check(act("type", target=1)) == ["text_missing"]


def test_type_text_and_param() -> None:
    d = act("type", target=1, text="x", param="text")
    assert check(d, subgoal_params=S1_PARAMS) == ["type_text_and_param"]


def test_type_free_text_ok() -> None:
    assert check(act("type", target=1, text="x")) == []


def test_type_param_ok_known_subgoal() -> None:
    assert check(act("type", target=1, param="text"), subgoal_params=S1_PARAMS) == []


def test_type_param_ok_declared_on_first_turn() -> None:
    d = Decision(
        subgoals=[SubgoalDoc(id="s1", desc="d", params={"text": "hello world"})],
        actions=[action("type", target=1, param="text")],
    )
    assert check(d, first_turn=True) == []


def test_param_unknown() -> None:
    assert check(act("type", target=1, param="nope"), subgoal_params=S1_PARAMS) == ["param_unknown"]
    assert check(act("type", target=1, param="text")) == ["param_unknown"]


def test_empty_decision() -> None:
    assert check(Decision()) == ["empty_decision"]


def test_ask_and_stop_alone_are_not_empty() -> None:
    assert check(Decision(ask="which?")) == []
    assert check(Decision(stop="blocked")) == []


def test_ask_with_actions() -> None:
    d = Decision(ask="q", actions=[action("click", target=1)])
    assert check(d) == ["ask_with_actions"]


def test_stop_with_actions() -> None:
    d = Decision(stop="cannot", actions=[action("click", target=1)])
    assert check(d) == ["stop_with_actions"]


def test_too_many_actions() -> None:
    d = Decision(actions=[action("click", target=1)] * 5)
    assert check(d) == ["too_many_actions"]
    assert check(Decision(actions=[action("click", target=1)] * 4)) == []


def test_too_many_subgoals() -> None:
    subs = [SubgoalDoc(id=f"s{i}", desc="d") for i in range(6)]
    d = Decision(subgoals=subs, actions=[action("click", target=1)])
    assert check(d, first_turn=True) == ["too_many_subgoals"]


def test_tokens_deduplicated_in_order() -> None:
    d = Decision(
        actions=[
            ActionDoc(verb="click", target=98, subgoal="s1"),
            ActionDoc(verb="click", target=99, subgoal="s1"),
        ]
    )
    assert check(d) == ["id_not_listed"]


def test_extra_field_forbidden_and_frozen() -> None:
    with pytest.raises(ValidationError):
        Decision.model_validate({"bogus": 1})
    d = Decision()
    with pytest.raises(ValidationError):
        d.ask = "x"
