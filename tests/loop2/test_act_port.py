"""Adapter slice 6: the act port (packet 2.5, section 3, rev 3.8 "Slice 6 design").

`ActPort.act(dispatch)` sends one action through v1's executors, over injected collaborators: the
world ports' last observation and live reads, a semantic executor and a physical one. These tests
use fakes that record every executor call, so "nothing sent" is checkable.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.executor import Action
from steerai.action.physical import PhysicalAction
from steerai.errors import ErrorCode
from steerai.loop2.act_port import ActPort
from steerai.loop2.loop import Dispatch, PortInterrupted
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from tests.loop2.world_fakes import FIELD, GO, PAGE, act, element, observed

BOX = element(42, "CheckBox", "Shop", (9, 42), 1)
COMBO = element(43, "ComboBox", "Size", (9, 43), 1)
SHOWN = observed([*PAGE, BOX, COMBO])


class Executor:
    """A fake executor: records each call and returns the scripted results in order."""

    def __init__(self, *results: ActionResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[Any, Snapshot]] = []

    def execute(self, action: Any, snapshot: Snapshot) -> ActionResult:  # Any: either kind
        self.calls.append((action, snapshot))
        return self.results.pop(0)


class World:
    """The world ports the act port reads: the last observation and the live reads."""

    def __init__(self, last: Observed | None = SHOWN) -> None:
        self.last = last
        self.values: dict[tuple[int, ...], ValueFact | None] = {}
        self.states: dict[tuple[tuple[int, ...], str], bool | None] = {}
        self.reads: list[str] = []

    def read_value(self, element: Element) -> ValueFact | None:
        self.reads.append("value")
        return self.values.get(element.runtime_id)

    def read_state(self, element: Element, wanted: str) -> bool | None:
        self.reads.append(f"state:{wanted}")
        return self.states.get((element.runtime_id, wanted))

    def fact_of(self, text: str) -> ValueFact:
        return ValueFact(filled=bool(text), digest=f"d:{text}")


def ok(verb: str) -> ActionResult:
    return ActionResult(verb=verb, delivery=Delivery.DELIVERED_VERIFIED)


def nothing_sent(verb: str, code: ErrorCode = ErrorCode.ELEMENT_NOT_ACTIONABLE) -> ActionResult:
    return ActionResult(verb=verb, delivery=Delivery.NOT_DELIVERED, code=code)


def uncertain(verb: str) -> ActionResult:
    return ActionResult(
        verb=verb, delivery=Delivery.DELIVERY_UNCERTAIN, code=ErrorCode.VERIFY_TIMEOUT
    )


def _dispatch(
    verb: str, target: Element | None, *, text: str | None = None, **kw: object
) -> Dispatch:
    action = act(verb, target=None if target is None else target.id, **kw)
    return Dispatch(
        action=action,
        runtime_id=() if target is None else target.runtime_id,
        role="" if target is None else target.role,
        name="" if target is None else target.name,
        risk="R0",
        provenance="goal_span" if verb == "type" else None,
        text=text,
    )


def _port(
    world: World | None = None,
    semantic: Executor | None = None,
    physical: Executor | None = None,
    interrupted: Any = None,  # Any: a switch reader, or None for one that never reports
) -> tuple[ActPort, World, Executor, Executor]:
    w = World() if world is None else world
    s = Executor() if semantic is None else semantic
    p = Executor() if physical is None else physical
    port = ActPort(
        world=w,
        semantic=s,
        physical=p,
        interrupted=(lambda: None) if interrupted is None else interrupted,
    )
    return port, w, s, p


# ---- nothing to send ---------------------------------------------------------------------------


def test_a_wait_sends_nothing_and_is_delivered() -> None:
    port, _, semantic, physical = _port()
    result = port.act(_dispatch("wait", None))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.read_back is None
    assert semantic.calls == physical.calls == []


def test_a_scroll_is_refused_and_sends_nothing() -> None:
    """[V2.41] (2): validate refuses it first; the act port is the second layer."""
    port, _, semantic, physical = _port()
    result = port.act(_dispatch("scroll", GO))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.action.code is ErrorCode.GATE_BLOCKED
    assert result.action.evidence["detail"] == "not_in_step"
    assert semantic.calls == physical.calls == []


def test_a_latched_kill_sends_nothing() -> None:
    port, _, semantic, physical = _port(
        semantic=Executor(ok("click")), interrupted=lambda: "killed"
    )
    with pytest.raises(PortInterrupted):
        port.act(_dispatch("click", GO))
    assert semantic.calls == physical.calls == []


# ---- resolve on the last observation, by RuntimeId, against the gate's identity ----------------


@pytest.mark.parametrize(
    "last",
    [
        None,
        observed([e for e in PAGE if e.id != GO.id]),  # gone
        observed([*PAGE, dataclasses.replace(GO, id=99)]),  # two elements hold its RuntimeId
    ],
)
def test_a_target_not_on_the_last_observation_sends_nothing(last: Observed | None) -> None:
    port, _, semantic, physical = _port(World(last))
    result = port.act(_dispatch("click", GO))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.action.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.action.evidence["detail"] == "target_gone"
    assert semantic.calls == physical.calls == []


@pytest.mark.parametrize(("role", "name"), [("Hyperlink", GO.name), (GO.role, "Delete all")])
def test_an_identity_other_than_the_gate_rated_sends_nothing(role: str, name: str) -> None:
    """RT-04: the element the act resolves must be the one the gate rated."""
    moved = dataclasses.replace(GO, role=role, name=name)
    last = observed([e if e.id != GO.id else moved for e in PAGE])
    port, _, semantic, physical = _port(World(last))
    result = port.act(_dispatch("click", GO))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.action.evidence["detail"] == "identity_changed"
    assert semantic.calls == physical.calls == []


def test_a_name_that_differs_only_in_folding_is_the_same_element() -> None:
    spaced = dataclasses.replace(GO, name="  Go ")
    last = observed([e if e.id != GO.id else spaced for e in PAGE])
    port, _, semantic, _ = _port(World(last), semantic=Executor(ok("click")))
    port.act(_dispatch("click", GO))
    assert len(semantic.calls) == 1


def test_the_action_goes_to_the_element_id_of_the_last_observation() -> None:
    """The model named a task handle; the executor gets the last observation's snapshot id."""
    relisted = dataclasses.replace(GO, id=30)
    last = observed([*(e for e in PAGE if e.id != GO.id), relisted])
    port, _, semantic, _ = _port(World(last), semantic=Executor(ok("click")))
    port.act(_dispatch("click", GO))
    sent, snapshot = semantic.calls[0]
    assert (sent.verb, sent.element_id) == ("click", 30)
    assert snapshot is last.snapshot


# ---- per verb ------------------------------------------------------------------------------------


def test_a_click_is_semantic_and_has_no_read_back() -> None:
    port, _, semantic, physical = _port(semantic=Executor(ok("click")))
    result = port.act(_dispatch("click", GO))
    assert isinstance(semantic.calls[0][0], Action)
    assert physical.calls == []
    assert (result.delivery, result.read_back) == (Delivery.DELIVERED_VERIFIED, None)


@pytest.mark.parametrize(
    ("verb", "wanted"),
    [("check", "on"), ("uncheck", "off"), ("select", "selected"), ("expand", "expanded"),
     ("collapse", "collapsed")],
)  # fmt: skip
@pytest.mark.parametrize("live", [True, False, None])
def test_a_state_verb_reads_its_state_back_live(verb: str, wanted: str, live: bool | None) -> None:
    world = World()
    world.states[(BOX.runtime_id, wanted)] = live
    port, _, semantic, _ = _port(world, semantic=Executor(ok(verb)))
    result = port.act(_dispatch(verb, BOX))
    assert semantic.calls[0][0].verb == verb
    assert result.read_back is live
    assert world.reads == [f"state:{wanted}"]


def test_choose_sends_the_option_and_reads_the_value_back() -> None:
    world = World()
    world.values[COMBO.runtime_id] = ValueFact(filled=True, digest="d:Large")
    port, _, semantic, _ = _port(world, semantic=Executor(ok("choose")))
    result = port.act(_dispatch("choose", COMBO, option="Large"))
    assert semantic.calls[0][0].option == "Large"
    assert result.read_back is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (ValueFact(filled=True, digest="d:red shoes"), True),
        (ValueFact(filled=True, digest="d:x"), False),
        (None, None),
    ],
)
def test_a_type_is_physical_and_reads_its_value_back(
    value: ValueFact | None, expected: bool | None
) -> None:
    world = World()
    world.values[FIELD.runtime_id] = value
    port, _, semantic, physical = _port(world, physical=Executor(ok("type")))
    result = port.act(_dispatch("type", FIELD, text="red shoes", param="text"))
    sent = physical.calls[0][0]
    assert isinstance(sent, PhysicalAction)
    assert (sent.verb, sent.text) == ("type", "red shoes")
    assert semantic.calls == []
    assert result.read_back is expected


def test_a_press_is_physical_with_the_key_as_its_chord() -> None:
    port, _, _, physical = _port(physical=Executor(ok("press")))
    port.act(_dispatch("press", FIELD, key="Enter"))
    sent = physical.calls[0][0]
    assert (sent.verb, sent.chord, sent.element_id) == ("press", "Enter", FIELD.id)


def test_a_press_with_the_live_focus_elsewhere_is_refused_298() -> None:
    """#298 ([V2.43] (1)): v1's press re-checks the live focus; FOCUS_LOST sends nothing and
    comes back as it is (the runner's world refusal)."""
    lost = nothing_sent("press", ErrorCode.FOCUS_LOST)
    port, _, _, physical = _port(physical=Executor(lost))
    result = port.act(_dispatch("press", FIELD, key="Enter"))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.action.code is ErrorCode.FOCUS_LOST
    assert len(physical.calls) == 1


def test_a_focus_is_a_physical_click() -> None:
    port, _, semantic, physical = _port(physical=Executor(ok("click")))
    port.act(_dispatch("focus", FIELD))
    assert physical.calls[0][0].verb == "click"
    assert semantic.calls == []


# ---- the physical fallback: only when the semantic call provably sent nothing ------------------


def test_a_click_that_sent_nothing_falls_back_to_the_physical_click() -> None:
    port, _, semantic, physical = _port(
        semantic=Executor(nothing_sent("click")), physical=Executor(ok("click"))
    )
    result = port.act(_dispatch("click", GO))
    assert len(semantic.calls) == len(physical.calls) == 1
    assert result.delivery is Delivery.DELIVERED_VERIFIED


def test_an_uncertain_click_never_falls_back() -> None:
    """[V2.40] Q6: a timeout or an error is uncertain delivery: it may have landed."""
    port, _, _, physical = _port(semantic=Executor(uncertain("click")))
    result = port.act(_dispatch("click", GO))
    assert physical.calls == []
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN


@pytest.mark.parametrize("verb", ["check", "select", "expand", "choose"])
def test_only_a_click_falls_back(verb: str) -> None:
    target = COMBO if verb == "choose" else BOX
    kw = {"option": "Large"} if verb == "choose" else {}
    port, _, _, physical = _port(semantic=Executor(nothing_sent(verb)))
    result = port.act(_dispatch(verb, target, **kw))
    assert physical.calls == []
    assert result.delivery is Delivery.NOT_DELIVERED


def test_nothing_sent_carries_no_read_back() -> None:
    world = World()
    world.states[(BOX.runtime_id, "on")] = True
    port, _, _, _ = _port(world, semantic=Executor(nothing_sent("check")))
    result = port.act(_dispatch("check", BOX))
    assert result.read_back is None
    assert world.reads == []
