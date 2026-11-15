"""Adapter slice 6: the act port (packet 2.5, section 3, rev 3.8 "Slice 6 design", rev 3.9 the
edge-case fold-in and [V2.51]).

`ActPort.act(dispatch)` sends one action through v1's executors, over injected collaborators: the
world ports' last observation and live reads, a semantic executor, a physical one and the desk
(v1's tap refusal, the person's last input, the live focus, the guard's arming). These tests use
fakes that record every executor call, so "nothing sent" is checkable.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery, Step
from steerai.action.executor import Action
from steerai.action.physical import PhysicalAction
from steerai.errors import ErrorCode
from steerai.loop2.act_port import ActPort
from steerai.loop2.loop import Dispatch, PortInterrupted
from steerai.orchestrator.loop import IDLE_S, MAX_WAIT_S
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from tests.action.fakes import UIA_E_ELEMENTNOTAVAILABLE, com_error
from tests.loop2.world_fakes import FIELD, GO, PAGE, PASSWORD, act, element, observed

BOX = element(42, "CheckBox", "Shop", (9, 42), 1)
COMBO = element(43, "ComboBox", "Size", (9, 43), 1)
COMBO_EDIT = element(44, "Edit", "Size", (9, 44), 43)
COMBO_ITEM = element(45, "ListItem", "Large", (9, 45), 43)
ROW = element(46, "TreeItem", "Docs", (9, 46), 1)
ROW_LABEL = element(47, "Text", "Docs", (9, 47), 46)
SHOWN = observed([*PAGE, BOX, COMBO, COMBO_EDIT, COMBO_ITEM, ROW, ROW_LABEL])


class Executor:
    """A fake executor: records each call (and whether the desk was armed then) and returns the
    scripted results in order."""

    def __init__(self, *results: ActionResult | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[Any, Snapshot]] = []
        self.armed: list[frozenset[tuple[int, ...]] | None] = []
        self.desk: Desk | None = None
        self.during: Callable[[], None] | None = None
        """Runs inside each call (the person's key pressed while the executor works)."""

    def execute(self, action: Any, snapshot: Snapshot) -> ActionResult:  # Any: either kind
        self.calls.append((action, snapshot))
        self.armed.append(None if self.desk is None else self.desk.current)
        if self.during is not None:
            self.during()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class World:
    """The world ports the act port reads: the last observation, a fresh look and live reads."""

    def __init__(self, last: Observed | None = SHOWN) -> None:
        self.last = last
        self.fresh: list[Observed] = []
        self.values: dict[tuple[int, ...], ValueFact | None] = {}
        self.states: dict[tuple[tuple[int, ...], str], bool | None] = {}
        self.secure: dict[tuple[int, ...], bool | None] = {}
        self.reads: list[str] = []
        self.looks = 0
        self.broken = False

    def observe(self) -> Observed:
        self.looks += 1
        if self.fresh:
            self.last = self.fresh.pop(0)
        if self.last is None:
            raise AssertionError("no observation")
        return self.last

    def read_value(self, element: Element) -> ValueFact | None:
        self.reads.append("value")
        return self.values.get(element.runtime_id)

    def read_state(self, element: Element, wanted: str) -> bool | None:
        if self.broken:
            raise RuntimeError("boom")
        self.reads.append(f"state:{wanted}")
        return self.states.get((element.runtime_id, wanted))

    def read_secure(self, element: Element) -> bool | None:
        return self.secure.get(element.runtime_id, False)

    def fact_of(self, text: str) -> ValueFact:
        return ValueFact(filled=bool(text), digest=f"d:{text}")


class Desk:
    """The desk: v1's tap refusal, the person's last input, the live focus and the arming."""

    def __init__(
        self,
        *,
        focus: tuple[int, ...] | None = FIELD.runtime_id,
        tap: str | None = None,
        last_input: float = -100.0,
    ) -> None:
        self.focus = focus
        self.focus_after: list[tuple[int, ...] | None] = []
        self.tap = tap
        self.last = last_input
        self.taps = 0
        self.current: frozenset[tuple[int, ...]] | None = None

    def tap_refusal(self) -> str | None:
        self.taps += 1
        return self.tap

    def last_input(self) -> float:
        return self.last

    def focused(self) -> tuple[int, ...] | None:
        if self.focus_after:
            self.focus = self.focus_after.pop(0)
        return self.focus

    def armed(self, focus: frozenset[tuple[int, ...]]) -> AbstractContextManager[None]:
        return self._arm(focus)

    @contextmanager
    def _arm(self, focus: frozenset[tuple[int, ...]]) -> Iterator[None]:
        self.current = focus
        try:
            yield
        finally:
            self.current = None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.pauses: list[float] = []
        self.on_pause: Callable[[], None] | None = None

    def __call__(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds
        if self.on_pause is not None:
            self.on_pause()


def ok(verb: str) -> ActionResult:
    return ActionResult(verb=verb, delivery=Delivery.DELIVERED_VERIFIED)


def unverified(verb: str) -> ActionResult:
    return ActionResult(
        verb=verb, delivery=Delivery.DELIVERED_UNVERIFIED, code=ErrorCode.VERIFY_TIMEOUT
    )


def nothing_sent(
    verb: str, code: ErrorCode = ErrorCode.ELEMENT_NOT_ACTIONABLE, detail: str = "disabled"
) -> ActionResult:
    return ActionResult(
        verb=verb, delivery=Delivery.NOT_DELIVERED, code=code, evidence={"detail": detail}
    )


NO_INVOKE = nothing_sent("click", detail="no_InvokePattern")


def uncertain(verb: str) -> ActionResult:
    return ActionResult(
        verb=verb, delivery=Delivery.DELIVERY_UNCERTAIN, code=ErrorCode.VERIFY_TIMEOUT
    )


def _dispatch(
    verb: str,
    target: Element | None,
    *,
    text: str | None = None,
    risk: str = "R0",
    own_text: bool = False,
    **kw: object,
) -> Dispatch:
    action = act(verb, target=None if target is None else target.id, **kw)
    return Dispatch(
        action=action,
        runtime_id=() if target is None else target.runtime_id,
        role="" if target is None else target.role,
        name="" if target is None else target.name,
        risk=risk,  # type: ignore[arg-type]  # a str: the tests drive the port's own risk check
        provenance="goal_span" if verb == "type" else None,
        text=text,
        own_text=own_text,
    )


@dataclasses.dataclass
class Rig:
    port: ActPort
    world: World
    semantic: Executor
    physical: Executor
    desk: Desk
    clock: Clock


def _port(
    world: World | None = None,
    *,
    semantic: Executor | None = None,
    physical: Executor | None = None,
    interrupted: Callable[[], str | None] | None = None,
    desk: Desk | None = None,
    wall_left: float = 600.0,
) -> Rig:
    w = World() if world is None else world
    s = Executor() if semantic is None else semantic
    p = Executor() if physical is None else physical
    d = Desk() if desk is None else desk
    p.desk = s.desk = d
    clock = Clock()
    port = ActPort(
        world=w,
        semantic=s,
        physical=p,
        desk=d,
        interrupted=(lambda: None) if interrupted is None else interrupted,
        clock=clock,
        wall_left=lambda: wall_left,
        pause=clock.pause,
    )
    return Rig(port, w, s, p, d, clock)


def _nothing_executed(rig: Rig) -> bool:
    return rig.semantic.calls == [] and rig.physical.calls == []


def _refused(result: Any, code: ErrorCode, detail: str) -> bool:  # Any: an ActResult
    return bool(
        result.delivery is Delivery.NOT_DELIVERED
        and result.action.code is code
        and result.action.evidence["detail"] == detail
        and result.action.steps == []
        and result.read_back is None
    )


TYPE: dict[str, Any] = {"text": "red shoes", "param": "text"}


# ---- nothing to send ---------------------------------------------------------------------------


def test_a_wait_sends_nothing_and_is_delivered() -> None:
    rig = _port()
    result = rig.port.act(_dispatch("wait", None))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.read_back is None
    assert _nothing_executed(rig)
    assert rig.desk.taps == 0


@pytest.mark.parametrize("verb", ["scroll", "open", "switch"])
def test_a_verb_not_in_the_step_is_refused_and_sends_nothing(verb: str) -> None:
    """[V2.41] (2), AP-26: validate refuses them first; the act port is the second layer."""
    rig = _port()
    result = rig.port.act(_dispatch(verb, GO))
    assert _refused(result, ErrorCode.GATE_BLOCKED, "not_in_step")
    assert _nothing_executed(rig)


def test_a_latched_kill_sends_nothing() -> None:
    rig = _port(semantic=Executor(ok("click")), interrupted=lambda: "killed")
    with pytest.raises(PortInterrupted):
        rig.port.act(_dispatch("click", GO))
    assert _nothing_executed(rig)


@pytest.mark.parametrize("risk", ["R2", "R3"])
def test_a_commit_is_refused_in_step_3(risk: str) -> None:
    """AP-13: step 3 dispatches only R0/R1; the runner ends the task first."""
    rig = _port(semantic=Executor(ok("click")))
    result = rig.port.act(_dispatch("click", GO, risk=risk))
    assert _refused(result, ErrorCode.GATE_BLOCKED, "commit_not_in_step")
    assert _nothing_executed(rig)


def test_text_on_a_verb_other_than_type_is_refused() -> None:
    """AP-26: the dispatch's shape, checked again: text only for type."""
    rig = _port(semantic=Executor(ok("click")))
    result = rig.port.act(_dispatch("click", GO, text="red shoes"))
    assert _refused(result, ErrorCode.GATE_BLOCKED, "shape")
    assert _nothing_executed(rig)


def test_an_option_on_a_verb_other_than_choose_is_refused() -> None:
    rig = _port(semantic=Executor(ok("click")))
    action = act("click", target=GO.id).model_copy(update={"option": "Large"})
    result = rig.port.act(dataclasses.replace(_dispatch("click", GO), action=action))
    assert _refused(result, ErrorCode.GATE_BLOCKED, "shape")
    assert _nothing_executed(rig)


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
    rig = _port(World(last))
    result = rig.port.act(_dispatch("click", GO))
    assert _refused(result, ErrorCode.ELEMENT_NOT_FOUND, "target_gone")
    assert _nothing_executed(rig)


def test_an_empty_runtime_id_is_target_gone() -> None:
    """AP-14: an element with no RuntimeId cannot be resolved, even if one is listed so."""
    blank = dataclasses.replace(GO, runtime_id=())
    rig = _port(World(observed([*PAGE[:2], blank])))
    result = rig.port.act(_dispatch("click", blank))
    assert _refused(result, ErrorCode.ELEMENT_NOT_FOUND, "target_gone")
    assert _nothing_executed(rig)


@pytest.mark.parametrize(("role", "name"), [("Hyperlink", GO.name), (GO.role, "Delete all")])
def test_an_identity_other_than_the_gate_rated_sends_nothing(role: str, name: str) -> None:
    """RT-04: the element the act resolves must be the one the gate rated."""
    moved = dataclasses.replace(GO, role=role, name=name)
    rig = _port(World(observed([e if e.id != GO.id else moved for e in PAGE])))
    result = rig.port.act(_dispatch("click", GO))
    assert _refused(result, ErrorCode.ELEMENT_NOT_FOUND, "identity_changed")
    assert _nothing_executed(rig)


@pytest.mark.parametrize("side", ["listed", "dispatched"])
def test_a_name_that_differs_only_in_folding_is_the_same_element(side: str) -> None:
    """Folded on both sides (A6): the snapshot's name and the dispatch's."""
    spaced = dataclasses.replace(GO, name="  Go ")
    listed = spaced if side == "listed" else GO
    rig = _port(
        World(observed([e if e.id != GO.id else listed for e in PAGE])),
        semantic=Executor(ok("click")),
    )
    rig.port.act(_dispatch("click", spaced if side == "dispatched" else GO))
    assert len(rig.semantic.calls) == 1


def test_the_action_goes_to_the_element_id_of_the_last_observation() -> None:
    """The model named a task handle; the executor gets the last observation's snapshot id."""
    relisted = dataclasses.replace(GO, id=30)
    last = observed([*(e for e in PAGE if e.id != GO.id), relisted])
    rig = _port(World(last), semantic=Executor(ok("click")))
    rig.port.act(_dispatch("click", GO))
    sent, snapshot = rig.semantic.calls[0]
    assert (sent.verb, sent.element_id) == ("click", 30)
    assert snapshot is last.snapshot


# ---- per verb ------------------------------------------------------------------------------------


def test_a_click_is_semantic_has_no_read_back_and_no_desk_check() -> None:
    rig = _port(semantic=Executor(ok("click")))
    result = rig.port.act(_dispatch("click", GO))
    assert isinstance(rig.semantic.calls[0][0], Action)
    assert rig.physical.calls == []
    assert (result.delivery, result.read_back) == (Delivery.DELIVERED_VERIFIED, None)
    assert rig.desk.taps == 0


@pytest.mark.parametrize(
    ("verb", "wanted"),
    [("check", "on"), ("uncheck", "off"), ("select", "selected"), ("expand", "expanded"),
     ("collapse", "collapsed")],
)  # fmt: skip
@pytest.mark.parametrize("live", [True, False, None])
def test_a_state_verb_reads_its_state_back_live(verb: str, wanted: str, live: bool | None) -> None:
    rig = _port(semantic=Executor(ok(verb)))
    rig.world.states[(BOX.runtime_id, wanted)] = live
    result = rig.port.act(_dispatch(verb, BOX))
    assert rig.semantic.calls[0][0].verb == verb
    assert result.read_back is live
    assert rig.world.reads == [f"state:{wanted}"]


def test_both_facts_are_kept_v1_verified_and_the_read_back_false() -> None:
    """[V2.51] (2): v1's delivery as reported, and the port's read-back; v2's verified is the
    read-back, so a False read-back after v1's verified stays a mismatch."""
    rig = _port(semantic=Executor(ok("check")))
    rig.world.states[(BOX.runtime_id, "on")] = False
    result = rig.port.act(_dispatch("check", BOX))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.read_back is False


def test_a_true_read_back_after_v1s_unverified_delivery_is_kept() -> None:
    rig = _port(semantic=Executor(unverified("check")))
    rig.world.states[(BOX.runtime_id, "on")] = True
    result = rig.port.act(_dispatch("check", BOX))
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.read_back is True


TOGGLED = Step(label="TogglePattern.Toggle", mechanism="semantic", outcome="succeeded")


def _guarded(detail: str | None) -> ActionResult:
    """v1's result when `ToggleGuard` raised at the second Toggle (or v1's poll read failed
    after the first): uncertain, phase read-back, the first Toggle's step only."""
    evidence: dict[str, Any] = {"reason": "verify_failed", "phase": "readback"}
    if detail is not None:
        evidence["detail"] = detail
    return ActionResult(
        verb="check",
        delivery=Delivery.DELIVERY_UNCERTAIN,
        steps=[TOGGLED],
        code=ErrorCode.VERIFY_FAILED,
        evidence=evidence,
    )


UNCHANGED = ActionResult(
    verb="check",
    delivery=Delivery.DELIVERED_UNVERIFIED,
    steps=[TOGGLED],
    code=ErrorCode.VERIFY_TIMEOUT,
    evidence={"toggles": 1, "detail": "unchanged"},
)


@pytest.mark.parametrize(
    "v1",
    [_guarded("second_toggle_refused"), UNCHANGED, _guarded(None)],
    ids=["moved", "unchanged", "unread"],
)
def test_no_second_toggle_is_one_delivered_toggle_refused(v1: ActionResult) -> None:
    """[V2.52] (a): unchanged from the initial state, unread, or moved off the third state: the
    first Toggle went and returned, the second never went (`second_toggle_refused`)."""
    rig = _port(semantic=Executor(v1))
    rig.world.states[(BOX.runtime_id, "on")] = False
    result = rig.port.act(_dispatch("check", BOX))
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.action.evidence == {"toggles": 1, "detail": "second_toggle_refused"}
    assert result.action.steps == [TOGGLED]
    assert result.read_back is False


def test_a_toggle_settled_on_the_re_read_is_verified() -> None:
    """Coordinator on [V2.52] (a): the re-read before the second Toggle showed the wanted state:
    no second Toggle, and a success."""
    rig = _port(semantic=Executor(_guarded("settled_on_reread")))
    rig.world.states[(BOX.runtime_id, "on")] = True
    result = rig.port.act(_dispatch("check", BOX))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.action.evidence == {"toggles": 1, "detail": "settled_on_reread"}
    assert result.action.steps == [TOGGLED]
    assert result.read_back is True


def test_an_unread_state_after_two_returned_toggles_stays_uncertain() -> None:
    """Both Toggles went and returned and the read after them failed: never "one Toggle"."""
    both = dataclasses.replace(_guarded(None), steps=[TOGGLED, TOGGLED])
    rig = _port(semantic=Executor(both))
    result = rig.port.act(_dispatch("check", BOX))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.action is both


def test_an_uncertain_toggle_with_no_step_returned_stays_uncertain() -> None:
    """A Toggle call that failed (no step succeeded) may have landed: never rewritten."""
    failed = dataclasses.replace(
        _guarded(None),
        steps=[Step(label="TogglePattern.Toggle", mechanism="semantic", outcome="failed")],
        evidence={"reason": "verify_failed", "phase": "call"},
    )
    rig = _port(semantic=Executor(failed))
    result = rig.port.act(_dispatch("check", BOX))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN


def test_choose_sends_the_option_and_reads_the_value_back() -> None:
    rig = _port(semantic=Executor(ok("choose")))
    rig.world.values[COMBO.runtime_id] = ValueFact(filled=True, digest="d:Large")
    result = rig.port.act(_dispatch("choose", COMBO, option="Large"))
    assert rig.semantic.calls[0][0].option == "Large"
    assert result.read_back is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (ValueFact(filled=True, digest="d:red shoes"), True),
        (ValueFact(filled=True, digest="d:x"), False),
        (None, None),
    ],
)
def test_a_type_is_physical_armed_and_reads_its_value_back(
    value: ValueFact | None, expected: bool | None
) -> None:
    rig = _port(physical=Executor(ok("type")))
    rig.world.values[FIELD.runtime_id] = value
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    sent = rig.physical.calls[0][0]
    assert isinstance(sent, PhysicalAction)
    assert (sent.verb, sent.text) == ("type", "red shoes")
    assert rig.physical.armed == [frozenset({FIELD.runtime_id})]
    assert rig.semantic.calls == []
    assert result.read_back is expected


def test_a_press_sends_the_closed_chord_armed_on_the_target_only() -> None:
    rig = _port(physical=Executor(ok("press")))
    rig.port.act(_dispatch("press", FIELD, key="Enter"))
    sent = rig.physical.calls[0][0]
    assert (sent.verb, sent.chord, sent.element_id) == ("press", "enter", FIELD.id)
    assert rig.physical.armed == [frozenset({FIELD.runtime_id})]


def test_a_key_outside_the_closed_map_is_refused() -> None:
    """AP-12: a key `validate` never let through, built past it, sends nothing."""
    action = act("press", target=FIELD.id, key="Enter").model_copy(update={"key": "F4"})
    sent = dataclasses.replace(_dispatch("press", FIELD, key="Enter"), action=action)
    rig = _port(physical=Executor(ok("press")))
    result = rig.port.act(sent)
    assert _refused(result, ErrorCode.GATE_BLOCKED, "key_not_allowed")
    assert _nothing_executed(rig)


@pytest.mark.parametrize(("own", "sent"), [(False, 0), (True, 1)])
def test_backspace_only_on_steers_own_text(own: bool, sent: int) -> None:
    """[V2.51] (5): the runner's press rule decides `own_text`; the port is the second layer."""
    rig = _port(physical=Executor(ok("press")))
    result = rig.port.act(_dispatch("press", FIELD, key="Backspace", own_text=own))
    assert len(rig.physical.calls) == sent
    if not own:
        assert _refused(result, ErrorCode.GATE_BLOCKED, "not_own_text")


@pytest.mark.parametrize("verb", ["type", "press"])
@pytest.mark.parametrize("focus", [GO.runtime_id, None, COMBO_EDIT.runtime_id])
def test_keys_go_only_where_the_live_focus_is_the_target(
    verb: str, focus: tuple[int, ...] | None
) -> None:
    """AP-10, AP-11: the live focus must be the target itself; unread refuses, and a descendant
    is never enough."""
    kw: dict[str, Any] = TYPE if verb == "type" else {"key": "Enter"}
    rig = _port(physical=Executor(ok(verb)), desk=Desk(focus=focus))
    result = rig.port.act(_dispatch(verb, FIELD, **kw))
    detail = "target_not_focused" if verb == "type" else "press_not_at_focus"
    assert _refused(result, ErrorCode.FOCUS_LOST, detail)
    assert _nothing_executed(rig)


def test_a_comboboxs_own_edit_takes_its_typing() -> None:
    rig = _port(physical=Executor(ok("type")), desk=Desk(focus=COMBO_EDIT.runtime_id))
    rig.port.act(_dispatch("type", COMBO, **TYPE))
    assert len(rig.physical.calls) == 1
    assert rig.physical.armed == [frozenset({COMBO.runtime_id, COMBO_EDIT.runtime_id})]


def test_a_comboboxs_other_child_is_not_its_field() -> None:
    rig = _port(physical=Executor(ok("type")), desk=Desk(focus=COMBO_ITEM.runtime_id))
    result = rig.port.act(_dispatch("type", COMBO, **TYPE))
    assert _refused(result, ErrorCode.FOCUS_LOST, "target_not_focused")


@pytest.mark.parametrize("verb", ["type", "press"])
def test_keys_never_go_to_a_listed_secure_field(verb: str) -> None:
    kw: dict[str, Any] = TYPE if verb == "type" else {"key": "Enter"}
    rig = _port(physical=Executor(ok(verb)), desk=Desk(focus=PASSWORD.runtime_id))
    result = rig.port.act(_dispatch(verb, PASSWORD, **kw))
    assert _refused(result, ErrorCode.GATE_BLOCKED, "secure_field")
    assert _nothing_executed(rig)


def test_keys_never_go_to_a_field_whose_secure_flag_was_not_read() -> None:
    """AP-09: `unread_secure` refuses before v1's admission."""
    last = dataclasses.replace(
        SHOWN, snapshot=dataclasses.replace(SHOWN.snapshot, unread_secure=frozenset({FIELD.id}))
    )
    rig = _port(World(last), physical=Executor(ok("type")))
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert _refused(result, ErrorCode.GATE_BLOCKED, "secure_unread")
    assert _nothing_executed(rig)


@pytest.mark.parametrize(("live", "detail"), [(True, "secure_field"), (None, "secure_unread")])
def test_keys_never_go_to_a_field_that_reads_secure_live(live: bool | None, detail: str) -> None:
    """AP-08: IsPassword read live before the first burst."""
    rig = _port(physical=Executor(ok("type")))
    rig.world.secure[FIELD.runtime_id] = live
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert _refused(result, ErrorCode.GATE_BLOCKED, detail)
    assert _nothing_executed(rig)


def test_type_without_text_fails_closed() -> None:
    rig = _port(physical=Executor(ok("type")))
    result = rig.port.act(_dispatch("type", FIELD, param="text"))
    assert _refused(result, ErrorCode.INVALID_TEXT, "text_missing")
    assert _nothing_executed(rig)


# ---- focus: fields only, with a three-valued read-back ([V2.51] (1)) ----------------------------


@pytest.mark.parametrize("target", [GO, BOX])
def test_focus_on_a_non_field_is_refused(target: Element) -> None:
    rig = _port(physical=Executor(ok("click")))
    result = rig.port.act(_dispatch("focus", target))
    assert _refused(result, ErrorCode.GATE_BLOCKED, "focus_needs_field")
    assert _nothing_executed(rig)


@pytest.mark.parametrize(
    ("after", "expected"),
    [(FIELD.runtime_id, True), (GO.runtime_id, False), (None, None)],
)
def test_focus_is_a_physical_click_read_back_by_the_live_focus(
    after: tuple[int, ...] | None, expected: bool | None
) -> None:
    desk = Desk(focus=after)
    rig = _port(physical=Executor(unverified("click")), desk=desk)
    result = rig.port.act(_dispatch("focus", FIELD))
    assert rig.physical.calls[0][0].verb == "click"
    assert rig.semantic.calls == []
    assert result.read_back is expected
    assert rig.desk.taps == 1  # physical input: the desk is read first


def test_the_focus_read_back_polls_until_the_focus_arrives() -> None:
    desk = Desk(focus=None)
    rig = _port(physical=Executor(unverified("click")), desk=desk)
    desk.focus_after = [GO.runtime_id, GO.runtime_id, FIELD.runtime_id]
    result = rig.port.act(_dispatch("focus", FIELD))
    assert result.read_back is True
    assert rig.clock.pauses
    assert max(rig.clock.pauses) <= 0.05


# ---- the desk before physical input (AP-03, AP-30) -----------------------------------------------


@pytest.mark.parametrize(
    ("tap", "code"),
    [
        ("secure_desktop", ErrorCode.SECURE_DESKTOP),
        ("hands_on", ErrorCode.GATE_BLOCKED),
        ("accessibility_filter", ErrorCode.GATE_BLOCKED),
        ("foreground_elevated", ErrorCode.GATE_BLOCKED),
        ("tap_unreadable", ErrorCode.GATE_BLOCKED),
    ],
)
def test_a_tap_refusal_sends_nothing_and_returns_its_token(tap: str, code: ErrorCode) -> None:
    rig = _port(physical=Executor(ok("type")), desk=Desk(tap=tap))
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert _refused(result, code, tap)
    assert _nothing_executed(rig)


def test_an_idle_person_means_no_wait_and_no_fresh_look() -> None:
    rig = _port(physical=Executor(ok("type")))
    rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert rig.clock.pauses == []
    assert rig.world.looks == 0


def test_a_busy_person_is_waited_out_then_a_fresh_look_is_taken() -> None:
    desk = Desk(last_input=0.0)
    rig = _port(physical=Executor(ok("type")), desk=desk)
    fresh = observed([*SHOWN.snapshot.elements])
    rig.world.fresh = [fresh]
    rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert rig.clock.now >= IDLE_S
    assert max(rig.clock.pauses) <= 0.05
    assert rig.world.looks == 1
    assert rig.physical.calls[0][1] is fresh.snapshot


def test_a_kill_while_waiting_for_the_person_sends_nothing() -> None:
    desk = Desk(last_input=0.0)
    latched: list[str] = []
    rig = _port(
        physical=Executor(ok("type")),
        desk=desk,
        interrupted=lambda: latched[0] if latched else None,
    )
    rig.clock.on_pause = lambda: latched.append("killed")
    with pytest.raises(PortInterrupted):
        rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert _nothing_executed(rig)


def test_a_person_busy_past_the_wait_is_a_takeover() -> None:
    desk = Desk(last_input=0.0)
    rig = _port(physical=Executor(ok("type")), desk=desk)
    rig.clock.on_pause = lambda: setattr(desk, "last", rig.clock.now)  # never idle
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert _refused(result, ErrorCode.GATE_PENDING_USER, "takeover")
    assert rig.clock.now == pytest.approx(MAX_WAIT_S, abs=0.06)
    assert _nothing_executed(rig)


def test_the_wait_for_the_person_stops_at_the_wall_budget() -> None:
    desk = Desk(last_input=0.0)
    rig = _port(physical=Executor(ok("type")), desk=desk, wall_left=1.0)
    rig.clock.on_pause = lambda: setattr(desk, "last", rig.clock.now)
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert _refused(result, ErrorCode.GATE_PENDING_USER, "wall_budget")
    assert rig.clock.now == pytest.approx(1.0, abs=0.06)


def test_a_screen_changed_during_the_wait_sends_nothing() -> None:
    desk = Desk(last_input=0.0)
    rig = _port(physical=Executor(ok("type")), desk=desk)
    other = observed([*SHOWN.snapshot.elements])
    retitled = dataclasses.replace(other.snapshot, title="Other")
    rig.world.fresh = [dataclasses.replace(other, snapshot=retitled)]
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert _refused(result, ErrorCode.FOCUS_LOST, "screen_changed_during_wait")
    assert _nothing_executed(rig)


def test_the_target_is_resolved_again_on_the_fresh_look() -> None:
    desk = Desk(last_input=0.0)
    rig = _port(physical=Executor(ok("type")), desk=desk)
    rig.world.fresh = [observed([e for e in SHOWN.snapshot.elements if e.id != FIELD.id])]
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert _refused(result, ErrorCode.ELEMENT_NOT_FOUND, "target_gone")
    assert _nothing_executed(rig)


# ---- the physical fallback: v1's rule exactly (AP-04, AP-05, AP-06) ------------------------------


def test_a_click_with_no_invoke_falls_back_to_one_physical_click() -> None:
    clicked = ActionResult(
        verb="click",
        delivery=Delivery.DELIVERED_UNVERIFIED,
        steps=[Step(label="mouse.click", mechanism="physical", outcome="succeeded")],
        code=ErrorCode.VERIFY_TIMEOUT,
    )
    rig = _port(semantic=Executor(NO_INVOKE), physical=Executor(clicked))
    result = rig.port.act(_dispatch("click", GO))
    assert len(rig.semantic.calls) == len(rig.physical.calls) == 1
    sent = rig.physical.calls[0][0]
    assert (sent.verb, sent.element_id) == ("click", GO.id)
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert [s.label for s in result.action.steps] == ["mouse.click"]
    assert result.action.evidence["click_fallback"] == "physical"
    assert result.action.evidence["semantic_detail"] == "no_InvokePattern"
    assert rig.desk.taps == 1


def test_the_fallback_aims_at_a_labelled_rows_own_label() -> None:
    rig = _port(semantic=Executor(NO_INVOKE), physical=Executor(ok("click")))
    rig.port.act(_dispatch("click", ROW))
    assert rig.physical.calls[0][0].element_id == ROW_LABEL.id


@pytest.mark.parametrize(
    "refusal",
    [
        nothing_sent("click", detail="disabled"),
        nothing_sent("click", ErrorCode.GATE_BLOCKED, detail="killed"),
        dataclasses.replace(
            NO_INVOKE,
            steps=[Step(label="InvokePattern.Invoke", mechanism="semantic", outcome="failed")],
        ),
    ],
)
def test_every_other_refusal_comes_back_as_it_is(refusal: ActionResult) -> None:
    rig = _port(semantic=Executor(refusal), physical=Executor(ok("click")))
    result = rig.port.act(_dispatch("click", GO))
    assert rig.physical.calls == []
    assert result.action is refusal


def test_no_fallback_onto_a_control_holding_an_acting_child() -> None:
    """v1's `physical_click_target`: the hit test accepts any descendant, so a row holding a
    button is never clicked in its middle (#84/#124)."""
    inner = element(48, "Button", "Delete", (9, 48), GO.id)
    rig = _port(
        World(observed([*SHOWN.snapshot.elements, inner])),
        semantic=Executor(NO_INVOKE),
        physical=Executor(ok("click")),
    )
    result = rig.port.act(_dispatch("click", GO))
    assert rig.physical.calls == []
    assert result.action is NO_INVOKE
    assert rig.desk.taps == 0  # v1's order: the target rule first, then the boundary


def test_no_fallback_when_an_acting_child_appeared_during_the_wait() -> None:
    desk = Desk(last_input=0.0)
    inner = element(48, "Button", "Delete", (9, 48), GO.id)
    rig = _port(semantic=Executor(NO_INVOKE), physical=Executor(ok("click")), desk=desk)
    rig.world.fresh = [observed([*SHOWN.snapshot.elements, inner])]
    result = rig.port.act(_dispatch("click", GO))
    assert rig.world.looks == 1
    assert rig.physical.calls == []
    assert result.action is NO_INVOKE


def test_a_tap_refusal_stops_the_fallback() -> None:
    rig = _port(
        semantic=Executor(NO_INVOKE), physical=Executor(ok("click")), desk=Desk(tap="hands_on")
    )
    result = rig.port.act(_dispatch("click", GO))
    assert rig.physical.calls == []
    assert _refused(result, ErrorCode.GATE_BLOCKED, "hands_on")


@pytest.mark.parametrize("verb", ["check", "select", "expand", "choose"])
def test_only_a_click_falls_back(verb: str) -> None:
    """On a tree row (a fallback role): the rule is the verb's, not the role's."""
    target = COMBO if verb == "choose" else ROW
    kw: dict[str, Any] = {"option": "Large"} if verb == "choose" else {}
    rig = _port(semantic=Executor(nothing_sent(verb, detail="no_InvokePattern")))
    result = rig.port.act(_dispatch(verb, target, **kw))
    assert rig.physical.calls == []
    assert result.delivery is Delivery.NOT_DELIVERED


@pytest.mark.parametrize("detail", [None, "no_InvokePattern"])
def test_an_uncertain_click_never_falls_back(detail: str | None) -> None:
    """[V2.40] Q6: a timeout or an error is uncertain delivery: it may have landed."""
    sent = uncertain("click")
    if detail is not None:
        sent = dataclasses.replace(sent, evidence={"detail": detail})
    rig = _port(semantic=Executor(sent))
    result = rig.port.act(_dispatch("click", GO))
    assert rig.physical.calls == []
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN


def test_nothing_sent_carries_no_read_back() -> None:
    rig = _port(semantic=Executor(nothing_sent("check")))
    rig.world.states[(BOX.runtime_id, "on")] = True
    result = rig.port.act(_dispatch("check", BOX))
    assert result.read_back is None
    assert rig.world.reads == []


# ---- the port never raises past the switch ([V2.51] (2)) ----------------------------------------


@pytest.mark.parametrize("where", ["semantic", "physical", "read_back"])
def test_any_error_inside_the_port_is_uncertain_delivery(where: str) -> None:
    boom = RuntimeError("boom")
    rig = _port(
        semantic=Executor(boom if where == "semantic" else ok("check")),
        physical=Executor(boom if where == "physical" else ok("type")),
    )
    rig.world.broken = where == "read_back"
    kw: dict[str, Any] = TYPE if where == "physical" else {}
    verb, target = ("type", FIELD) if where == "physical" else ("check", BOX)
    result = rig.port.act(_dispatch(verb, target, **kw))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.action.evidence == {"detail": "port_error", "error": "RuntimeError"}
    assert result.read_back is None


# ---- #352 review round --------------------------------------------------------------------------


def test_a_semantic_verb_waits_for_the_person_too_with_no_tap() -> None:
    """v1's `_boundary` runs before every dispatch (orchestrator/loop.py `_act`): the idle rule
    and the fresh look before a semantic click as well; the tap refusal is physical only."""
    desk = Desk(last_input=0.0, tap="hands_on")
    rig = _port(semantic=Executor(ok("click")), desk=desk)
    fresh = observed([*SHOWN.snapshot.elements])
    rig.world.fresh = [fresh]
    rig.port.act(_dispatch("click", GO))
    assert rig.clock.now >= IDLE_S
    assert rig.world.looks == 1
    assert rig.semantic.calls[0][1] is fresh.snapshot
    assert desk.taps == 0


def test_a_kill_during_the_semantic_call_stops_the_fallback_with_no_click() -> None:
    """Principal M10: the switch latches inside the semantic call (no Invoke); the person is
    idle, so the fallback's boundary must read the switch before its idle check."""
    latched: list[str] = []
    rig = _port(
        semantic=Executor(NO_INVOKE),
        physical=Executor(ok("click")),
        interrupted=lambda: latched[0] if latched else None,
    )
    rig.semantic.during = lambda: latched.append("killed")
    with pytest.raises(PortInterrupted):
        rig.port.act(_dispatch("click", GO))
    assert rig.physical.calls == []


def test_a_fresh_look_in_another_window_with_the_same_title_sends_nothing() -> None:
    """Principal M17: the window handle is checked, not only the title."""
    desk = Desk(last_input=0.0)
    rig = _port(physical=Executor(ok("type")), desk=desk)
    other = observed([*SHOWN.snapshot.elements])
    rig.world.fresh = [
        dataclasses.replace(other, snapshot=dataclasses.replace(other.snapshot, hwnd=0x99))
    ]
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert _refused(result, ErrorCode.FOCUS_LOST, "screen_changed_during_wait")
    assert _nothing_executed(rig)


def test_a_focus_on_a_comboboxs_own_edit_reads_back_true() -> None:
    """Principal item 3: the read-back agrees with where keys go (`key_targets`)."""
    rig = _port(physical=Executor(unverified("click")), desk=Desk(focus=COMBO_EDIT.runtime_id))
    result = rig.port.act(_dispatch("focus", COMBO))
    assert result.read_back is True


def test_a_tree_item_expand_by_click_is_deferred_and_sends_nothing() -> None:
    """Packet §3 / [V2.40] Q6, deferred (AP-25, #353): v1 asks for a click; the port refuses."""
    asks_click = nothing_sent("expand", detail="expand_by_click")
    rig = _port(semantic=Executor(asks_click), physical=Executor(ok("click")))
    result = rig.port.act(_dispatch("expand", ROW))
    assert _refused(result, ErrorCode.ELEMENT_NOT_ACTIONABLE, "expand_needs_click")
    assert rig.physical.calls == []


def test_a_kill_ends_the_focus_poll_at_once() -> None:
    """The tester's m26: the poll reads the switch at each step (latency, not safety)."""
    flag: list[bool] = []
    rig = _port(
        physical=Executor(unverified("click")),
        desk=Desk(focus=GO.runtime_id),
        interrupted=lambda: "takeover" if flag else None,
    )
    rig.clock.on_pause = lambda: flag.append(True)
    result = rig.port.act(_dispatch("focus", FIELD))
    assert result.read_back is False
    assert result.delivery is not Delivery.NOT_DELIVERED
    assert len(rig.clock.pauses) == 1


def test_an_error_after_a_send_keeps_what_v1_sent() -> None:
    """Principal non-blocking: a `port_error` after the executor returned keeps its steps."""
    toggled = dataclasses.replace(ok("check"), steps=[TOGGLED])
    rig = _port(semantic=Executor(toggled))
    rig.world.broken = True
    result = rig.port.act(_dispatch("check", BOX))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.action.steps == [TOGGLED]


def test_a_second_toggle_that_may_have_landed_stays_uncertain() -> None:
    """Principal item 1: `ToggleGuard` names a raising second Toggle; it is never refused."""
    may_have = _guarded("second_toggle_uncertain")
    rig = _port(semantic=Executor(may_have))
    result = rig.port.act(_dispatch("check", BOX))
    assert result.action is may_have


def test_v1s_unchanged_keeps_its_code() -> None:
    rig = _port(semantic=Executor(UNCHANGED))
    rig.world.states[(BOX.runtime_id, "on")] = False
    result = rig.port.act(_dispatch("check", BOX))
    assert result.action.code is ErrorCode.VERIFY_TIMEOUT


def test_the_fallback_records_where_it_aimed() -> None:
    rig = _port(semantic=Executor(NO_INVOKE), physical=Executor(ok("click")))
    assert rig.port.act(_dispatch("click", ROW)).action.evidence["aim"] == "label"

def test_a_raw_com_error_inside_the_port_keeps_its_taxonomy_code() -> None:
    """AP-27: a COMError the executor did not classify goes through `execution.code_for`."""
    rig = _port(semantic=Executor(com_error(UIA_E_ELEMENTNOTAVAILABLE)))
    result = rig.port.act(_dispatch("click", GO))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.action.evidence == {
        "detail": "port_error",
        "error": "ComError",
        "code": "element_not_found",
    }
