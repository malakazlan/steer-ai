"""[V2.85] H2 guard: a value read from a control whose state is expanded proves nothing. It covers
the value claim (`control_expanded`), the act port's read-back after a choose and its
'already satisfied' skip. Code never collapses the control."""

from __future__ import annotations

from steerai.action.dispositions import ActionResult, Delivery
from steerai.loop2.controller import REPAIR_LEGEND, SYSTEM_RULES
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.history import CLAIM_RESULTS
from steerai.loop2.runner import run_task
from steerai.world.effects import ValueFact
from tests.loop2.test_act_port import COMBO, Executor, _dispatch, _port, ok
from tests.loop2.world_fakes import PAGE, World, act, element, observed, task

SAID = ValueFact(filled=True, digest="d:red shoes")
BOX = element(42, "ComboBox", "Shop", (9, 42), 1)
SHOWN = [*PAGE, BOX]


def _claim_world(expanded: bool | None) -> World:
    sub = SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="value", field="shop")
    turns = [
        Decision(subgoals=[sub], actions=[act("wait", subgoal="s1")]),
        Decision(claims=[Claim(subgoal="s1", kind="value", evidence=[BOX.id], param="text")]),
        Decision(stop="blocked"),
    ]
    world = World(turns, screen=observed(SHOWN))
    world.values[BOX.runtime_id] = SAID
    world.fact_text = True
    world.states[(BOX.runtime_id, "expanded")] = expanded
    return world


def test_a_value_claim_on_an_expanded_control_is_not_proven() -> None:
    world = _claim_world(True)
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert "claim:s1:control_expanded" in world.views[-1].history
    assert not any("proven:s1" in view.history for view in world.views)


def test_a_value_claim_on_a_collapsed_or_unexpandable_control_is_unchanged() -> None:
    for expanded in (False, None):
        assert run_task(task(), _claim_world(expanded).ports()).status == "done"


def test_the_token_is_a_claim_check_and_taught_by_a_legend_line() -> None:
    assert "control_expanded" in CLAIM_RESULTS
    assert "control_expanded" in SYSTEM_RULES
    assert "a value read while its list is open proves nothing; close it first" in SYSTEM_RULES
    assert "control_expanded" not in REPAIR_LEGEND


def test_a_choose_read_back_on_an_expanded_control_is_not_verified() -> None:
    rig = _port(semantic=Executor(ok("choose")))
    rig.world.values[COMBO.runtime_id] = ValueFact(filled=True, digest="d:Large")
    rig.world.states[(COMBO.runtime_id, "expanded")] = True
    result = rig.port.act(_dispatch("choose", COMBO, option="Large"))
    assert result.read_back is None
    assert len(rig.semantic.calls) == 1  # nothing was sent to collapse it


def test_a_choose_read_back_on_a_collapsed_control_is_unchanged() -> None:
    rig = _port(semantic=Executor(ok("choose")))
    rig.world.values[COMBO.runtime_id] = ValueFact(filled=True, digest="d:Large")
    rig.world.states[(COMBO.runtime_id, "expanded")] = False
    assert rig.port.act(_dispatch("choose", COMBO, option="Large")).read_back is True


def test_an_already_satisfied_choose_on_an_expanded_control_is_not_a_skip() -> None:
    rig = _port(semantic=Executor(ActionResult.already_satisfied("choose")))
    rig.world.states[(COMBO.runtime_id, "expanded")] = True
    result = rig.port.act(_dispatch("choose", COMBO, option="Large"))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert not result.action.skipped
    assert result.action.evidence["detail"] == "control_expanded"


def test_an_already_satisfied_choose_on_a_collapsed_control_stays_a_skip() -> None:
    rig = _port(semantic=Executor(ActionResult.already_satisfied("choose")))
    rig.world.states[(COMBO.runtime_id, "expanded")] = False
    assert rig.port.act(_dispatch("choose", COMBO, option="Large")).action.skipped
