"""[V2.85] H2 guard: a value read from a control whose list is open, or whose expand state could
not be read, proves nothing. An element that lists expanded or collapsed (or any element when
caps were not read) must read Collapsed live: False (open, or PartiallyExpanded) is
`control_expanded`, None (no pattern, or a failed read) is unread. Covers the value claim, the
closing re-read, the act port's read-back after a choose and its 'already satisfied' skip. Code
never collapses the control."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.loop2.controller import REPAIR_LEGEND, SYSTEM_RULES
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.history import CLAIM_RESULTS
from steerai.loop2.runner import run_task
from steerai.world.effects import ValueFact
from tests.loop2.test_act_port import COMBO, Executor, _dispatch, _port, ok
from tests.loop2.test_act_port import SHOWN as ACT_SHOWN
from tests.loop2.world_fakes import PAGE, World, act, element, observed, task

SAID = ValueFact(filled=True, digest="d:red shoes")
BOX = dataclasses.replace(element(42, "ComboBox", "Shop", (9, 42), 1), caps=("collapsed",))
PLAIN = element(43, "Edit", "Shop", (9, 43), 1)
COMBO_LISTING = dataclasses.replace(COMBO, caps=("expanded",))


def _sub() -> SubgoalDoc:
    return SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="value", field="shop")


def _world(box: Any, collapsed: bool | None, *, caps_read: bool = True) -> World:
    turns = [
        Decision(subgoals=[_sub()], actions=[act("wait", subgoal="s1")]),
        Decision(claims=[Claim(subgoal="s1", kind="value", evidence=[box.id], param="text")]),
        Decision(stop="blocked"),
    ]
    screen = observed([*PAGE, box])
    screen = dataclasses.replace(
        screen, snapshot=dataclasses.replace(screen.snapshot, caps_read=caps_read)
    )
    world = World(turns, screen=screen)
    world.values[box.runtime_id] = SAID
    world.fact_text = True
    world.states[(box.runtime_id, "collapsed")] = collapsed
    return world


def test_a_value_claim_on_an_open_list_is_not_proven() -> None:
    for collapsed in (False,):  # False: expanded, or PartiallyExpanded (neither state)
        world = _world(BOX, collapsed)
        outcome = run_task(task(), world.ports())
        assert outcome.status != "done"
        assert "claim:s1:control_expanded" in world.views[-1].history
        assert outcome.field_mismatch == 0  # the field was held; the list was open


def test_a_value_claim_on_a_list_that_cannot_be_read_is_value_unread() -> None:
    world = _world(BOX, None)
    assert run_task(task(), world.ports()).status != "done"
    assert "claim:s1:value_unread" in world.views[-1].history


def test_an_unread_caps_snapshot_still_needs_the_collapsed_read() -> None:
    assert run_task(task(), _world(PLAIN, False, caps_read=False).ports()).status != "done"
    assert run_task(task(), _world(PLAIN, True, caps_read=False).ports()).status == "done"


def test_a_collapsed_control_and_a_field_with_no_list_are_unchanged() -> None:
    assert run_task(task(), _world(BOX, True).ports()).status == "done"
    assert run_task(task(), _world(PLAIN, None).ports()).status == "done"  # caps read, no list


class _Sequence(dict):  # type: ignore[type-arg]  # a test double: reads answer from a script
    def __init__(self, script: dict[tuple[tuple[int, ...], str], list[bool | None]]) -> None:
        super().__init__()
        self.script = script

    def get(self, key: Any, default: Any = None) -> Any:  # Any: dict's own signature
        queue = self.script.get(key)
        return queue.pop(0) if queue else default


def test_a_two_param_subgoal_does_not_close_when_the_first_list_opened_by_the_second_claim() -> (
    None
):
    """Kills the `_still` mutation: the first param's element reads open at the closing re-read."""
    first = dataclasses.replace(element(42, "ComboBox", "Shop", (9, 42), 1), caps=("collapsed",))
    second = element(43, "Edit", "Shop", (9, 43), 1)
    sub = SubgoalDoc(
        id="s1", desc="x", params={"a": "red", "b": "shoes"}, closes="value", field="unnamed"
    )
    claims = [
        Claim(subgoal="s1", kind="value", evidence=[first.id], param="a"),
        Claim(subgoal="s1", kind="value", evidence=[second.id], param="b"),
    ]
    world = World(
        [Decision(subgoals=[sub], claims=claims), Decision(stop="blocked")],
        screen=observed([*PAGE, first, second]),
    )
    world.values[first.runtime_id] = ValueFact(filled=True, digest="d:red")
    world.values[second.runtime_id] = ValueFact(filled=True, digest="d:shoes")
    world.fact_text = True
    world.states = _Sequence({(first.runtime_id, "collapsed"): [True, False]})
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert not any("proven:s1" in view.history for view in world.views)


def test_the_token_is_a_claim_result_and_taught() -> None:
    assert "control_expanded" in CLAIM_RESULTS
    assert "a value read while its list is open proves nothing; close it first" in SYSTEM_RULES
    assert "control_expanded" not in REPAIR_LEGEND


def _rig(collapsed: bool | None, result: ActionResult | None = None) -> Any:  # Any: the test Rig
    rig = _port(semantic=Executor(result or ok("choose")))
    rig.world.last = dataclasses.replace(
        ACT_SHOWN,
        snapshot=dataclasses.replace(
            ACT_SHOWN.snapshot,
            elements=[
                COMBO_LISTING if e.id == COMBO.id else e for e in ACT_SHOWN.snapshot.elements
            ],
        ),
    )
    rig.world.values[COMBO.runtime_id] = ValueFact(filled=True, digest="d:Large")
    rig.world.states[(COMBO.runtime_id, "collapsed")] = collapsed
    return rig


@pytest.mark.parametrize(("collapsed", "expected"), [(True, True), (False, None), (None, None)])
def test_a_choose_read_back_needs_a_list_read_closed(
    collapsed: bool | None, expected: bool | None
) -> None:
    rig = _rig(collapsed)
    result = rig.port.act(_dispatch("choose", COMBO, option="Large"))
    assert result.read_back is expected
    assert len(rig.semantic.calls) == 1  # nothing was sent to close it


@pytest.mark.parametrize(
    ("collapsed", "detail"), [(False, "control_expanded"), (None, "unreadable_value")]
)
def test_an_already_satisfied_choose_on_an_open_or_unread_list_is_not_a_skip(
    collapsed: bool | None, detail: str
) -> None:
    rig = _rig(collapsed, ActionResult.already_satisfied("choose"))
    result = rig.port.act(_dispatch("choose", COMBO, option="Large"))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert not result.action.skipped
    assert result.action.evidence["detail"] == detail


def test_an_already_satisfied_choose_on_a_collapsed_list_stays_a_skip() -> None:
    rig = _rig(True, ActionResult.already_satisfied("choose"))
    assert rig.port.act(_dispatch("choose", COMBO, option="Large")).action.skipped
