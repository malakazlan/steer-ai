"""[V2.84] (1): a value subgoal declares the field the words belong in, before any action. Code
checks the field words are a span of the request; the value proof also needs the cited element's
name or its group name to hold them (`field_mismatch`), `unnamed` opting out. Nearby text never
counts."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.loop2.controller import (
    REPAIR_LEGEND,
    SYSTEM_RULES,
    _prompt_schema,
    build_intake_prompt,
)
from steerai.loop2.decision import Claim, Decision, SubgoalDoc, validate
from steerai.loop2.loop import ProofLine, TaskOutcome
from steerai.loop2.runner import REFUSAL_KIND, run_task
from steerai.world.effects import ValueFact
from tests.loop2.world_fakes import GOAL, PAGE, World, act, element, observed, task

NEWLINE = chr(10)
SAID = ValueFact(filled=True, digest="d:red shoes")
WRONG = element(42, "Edit", "Email", (9, 42), 1)
RIGHT = element(43, "Edit", "Shop", (9, 43), 1)
GROUP = element(44, "Group", "Shop", (9, 44), 1)
IN_GROUP = element(45, "Edit", "", (9, 45), 44)
NEARBY = element(46, "Text", "Shop", (9, 46), 1)
UNNAMED = element(47, "Edit", "", (9, 47), 1)
ALL = [*PAGE, WRONG, RIGHT, GROUP, IN_GROUP, NEARBY, UNNAMED]


def _sub(field: str | None) -> SubgoalDoc:
    return SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="value", field=field)


def _world(field: str | None, cite: int) -> World:
    turns = [
        Decision(subgoals=[_sub(field)], actions=[act("wait", subgoal="s1")]),
        Decision(claims=[Claim(subgoal="s1", kind="value", evidence=[cite], param="text")]),
        Decision(stop="blocked"),
    ]
    world = World(turns, screen=observed(ALL))
    for box in ALL:
        world.values[box.runtime_id] = SAID
    world.fact_text = True
    return world


def _proven(world: World) -> bool:
    return any("proven:s1" in view.history for view in world.views)


def test_the_words_typed_into_the_wrong_field_are_not_proven_and_never_done() -> None:
    world = _world("shop", WRONG.id)
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert not _proven(world)
    assert "claim:s1:field_mismatch" in world.views[-1].history
    assert outcome.field_mismatch == 1


@pytest.mark.parametrize("cite", [RIGHT.id, IN_GROUP.id], ids=["by_name", "by_group_name"])
def test_the_right_field_by_its_name_or_its_group_name_is_proven(cite: int) -> None:
    world = _world("shop", cite)
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert outcome.field_mismatch == 0


def test_nearby_text_never_holds_the_field() -> None:
    """UNNAMED has no name and no named group; a Text beside it says "Shop" but is no label."""
    outcome = run_task(task(), _world("shop", UNNAMED.id).ports())
    assert outcome.status != "done"
    assert outcome.field_mismatch == 1


def test_an_unnamed_field_declaration_behaves_as_before() -> None:
    outcome = run_task(task(), _world("unnamed", WRONG.id).ports())
    assert outcome.status == "done"
    assert outcome.field_mismatch == 0


def _first(**sub: object) -> list[str]:
    doc = {"id": "s1", "desc": "d", "params": {"a": "red"}, "closes": "value"} | sub
    decision = Decision(subgoals=[SubgoalDoc.model_validate(doc)], stop="cannot")
    return list(validate(decision, listed_ids=frozenset(), goal=GOAL, first_turn=True))


def test_a_value_subgoal_without_field_is_field_missing() -> None:
    assert _first() == ["field_missing"]
    assert _first(field="shop") == []
    assert _first(field="unnamed") == []


@pytest.mark.parametrize("closes", ["state", "page"])
def test_a_non_value_subgoal_with_field_is_field_not_value(closes: str) -> None:
    extra = {"wanted": "on"} if closes == "state" else {}
    assert _first(closes=closes, field="shop", **extra) == ["field_not_value"]


@pytest.mark.parametrize("words", ["", "gizmo", "red the"])
def test_field_words_that_are_not_a_span_of_the_request_are_field_not_span(words: str) -> None:
    assert _first(field=words) == ["field_not_span"]


def test_the_tokens_are_closed_model_refusals_and_taught() -> None:
    for token in ("field_missing", "field_not_value", "field_not_span"):
        assert REFUSAL_KIND[token] == "model"
        assert token in REPAIR_LEGEND
    assert "field_mismatch" in SYSTEM_RULES
    assert "field" in SYSTEM_RULES
    assert "field" in _prompt_schema(first_turn=True)["$defs"]["SubgoalDoc"]["properties"]


def test_the_intake_sees_each_value_subgoals_field() -> None:
    _system, user = build_intake_prompt(
        GOAL, ((1, "value", ((1, "red shoes"),), None, "shop"),), task_fence="taskfence01"
    )
    assert "field=shop" in NEWLINE.join(user)


def test_the_intake_memo_key_holds_the_field() -> None:
    from tests.loop2.test_intake_check import _first as first  # noqa: PLC0415

    world = _world_with_two_fields(first)
    run_task(task(), world.ports())
    assert [view.subgoals[0][4] for view in world.intakes] == ["shop", "unnamed"]


def _world_with_two_fields(first: object) -> World:
    from tests.loop2.test_auto_proof_every_observation import RESULTS, START  # noqa: PLC0415
    from tests.loop2.test_intake_check import _world_with  # noqa: PLC0415

    subs = [[_sub("shop")], [_sub("unnamed")]]
    world = _world_with(
        [first(s) for s in subs] + [Decision(stop="blocked")]  # type: ignore[operator]  # test helper
    )
    world.screen = START
    world.observations = [START, RESULTS]
    return world


def test_the_outcome_carries_the_count() -> None:
    assert "field_mismatch" in {f.name for f in dataclasses.fields(TaskOutcome)}


# ---- principal review: audit packet, group rule, fail closed, the re-read ----------------------

GROUPED = element(48, "Edit", "Name", (9, 48), 49)
EMAIL_GROUP = element(49, "Group", "Email preferences", (9, 49), 1)
SECOND = element(50, "Edit", "Shop", (9, 50), 1)


def test_the_audit_packet_shows_the_field_on_a_value_proof_line() -> None:
    from steerai.loop2.controller import AUDIT_RULES, build_audit_prompt  # noqa: PLC0415

    def packet(field: str | None) -> str:
        line: ProofLine = (
            1, "value", "claim", "value", ("value_read", "equals_param"), ((1, "red shoes"),), field
        )  # fmt: skip
        _, user = build_audit_prompt(
            GOAL, (line,), "w", [], task_fence="taskfence01", screen_fence="scrfence01"
        )
        return NEWLINE.join(user)

    assert "field=shop" in packet("shop")
    assert "field=unnamed" in packet("unnamed")
    assert "field=" not in packet(None)
    assert "names a field" in AUDIT_RULES


def test_a_group_name_counts_only_for_an_element_with_no_name_of_its_own() -> None:
    from steerai.loop2.task import Subgoal  # noqa: PLC0415
    from steerai.loop2.verify import field_held  # noqa: PLC0415

    shot = observed([*PAGE, GROUPED, EMAIL_GROUP, IN_GROUP, GROUP]).snapshot
    sub = Subgoal(id="s1", desc="x", params={}, closes="value", field="email")
    assert not field_held(shot, GROUPED, sub)  # named "Name" inside "Email preferences"
    assert field_held(shot, IN_GROUP, dataclasses.replace(sub, field="shop"))  # no name of its own


def test_a_value_subgoal_with_no_field_fails_closed() -> None:
    from steerai.loop2.task import Subgoal  # noqa: PLC0415
    from steerai.loop2.verify import field_held  # noqa: PLC0415

    shot = observed(ALL).snapshot
    assert not field_held(shot, RIGHT, Subgoal(id="s1", desc="x", params={}, closes="value"))


def test_a_first_proof_whose_element_stops_holding_the_field_does_not_close_the_subgoal() -> None:
    doc = SubgoalDoc(
        id="s1", desc="x", params={"a": "red", "b": "shoes"}, closes="value", field="shop"
    )
    renamed = dataclasses.replace(RIGHT, name="Email")
    turns = [
        Decision(subgoals=[doc], actions=[act("wait", subgoal="s1")]),
        Decision(
            claims=[Claim(subgoal="s1", kind="value", evidence=[RIGHT.id], param="a")],
            actions=[act("wait", subgoal="s1")],
        ),
        Decision(claims=[Claim(subgoal="s1", kind="value", evidence=[SECOND.id], param="b")]),
        Decision(stop="blocked"),
    ]
    world = World(turns, screen=observed([*PAGE, RIGHT, SECOND]))
    world.later = observed([*PAGE, renamed, SECOND])
    world.later_after_acts = 2  # after the first claim was accepted
    world.values[RIGHT.runtime_id] = ValueFact(filled=True, digest="d:red")
    world.values[SECOND.runtime_id] = ValueFact(filled=True, digest="d:shoes")
    world.fact_text = True
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert any("claim:s1:accepted" in view.history for view in world.views)
    assert outcome.field_mismatch == 1  # the re-read of the first proof, counted
