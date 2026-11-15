"""The act port (adapter slice 6; packet 2.5, section 3, rev 3.8 "Slice 6 design").

`ActPort.act(dispatch)` sends one action through v1's executors, over injected collaborators so it
runs and is tested off Windows: the world ports (the last observation handed out and the live
reads), a semantic executor (`action.executor.Executor`) and a physical one
(`action.physical.PhysicalExecutor`). Slice 7 builds both executors with the adapter's one
monotonic clock and with `blocker = interrupted`, so v1's `Call` reads the switch before every
input burst and every pattern call: nothing is sent after the key.

The order of every act:
1. the switch, read once more before anything is sent;
2. nothing to send: a `wait` actuates nothing (the change-aware watch after it IS the wait), and a
   `scroll` is refused defensively, behind `validate` ([V2.41] (2));
3. resolve on the last observation by RuntimeId (exactly one element), and check it is the
   element the gate rated (`Dispatch.role` / `Dispatch.name`, RT-04): else nothing is sent;
4. the executor for the verb (section 3); v1's `Admission` then re-checks the live element;
5. a click whose semantic call provably sent nothing (`not_delivered`) falls back to one physical
   click ([V2.40] Q6); a timeout or an error is uncertain delivery and comes back as it is;
6. the read-back of a read-back verb, attached only when something was sent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final, Protocol, TypeVar

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.executor import Action
from steerai.action.physical import PhysicalAction
from steerai.errors import ErrorCode
from steerai.loop2.loop import ActResult, Dispatch, PortInterrupted
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from steerai.world.model import Snapshot, normalise_name

A_contra = TypeVar("A_contra", contravariant=True)

SEMANTIC_VERBS: Final = frozenset(
    {"click", "check", "uncheck", "select", "expand", "collapse", "choose"}
)
"""Sent through the UIA patterns (`Executor`), section 3."""
PHYSICAL_VERBS: Final = frozenset({"type", "press", "focus"})
"""Sent as input (`PhysicalExecutor`, every event tagged with Steer's `dwExtraInfo`); `focus` is a
hit-tested click, as in v1."""
FALLBACK_VERBS: Final = frozenset({"click"})
"""[V2.40] Q6, the conservative reading: only a click falls back to the physical path, and only
when its semantic call provably sent nothing. check, uncheck, select, expand, collapse and choose
never fall back (a physical click on a state element is a different action: it may toggle twice
or pick another option); type, press and focus are physical already."""
WANTED: Final[Mapping[str, str]] = {
    "check": "on",
    "uncheck": "off",
    "select": "selected",
    "expand": "expanded",
    "collapse": "collapsed",
}
"""The live state each state verb reads back ([V2.29] Q2)."""


class Reads(Protocol):
    """What the act port reads from the world ports (slice 5's `WorldPorts`)."""

    last: Observed | None

    def read_value(self, element: Element) -> ValueFact | None: ...
    def read_state(self, element: Element, wanted: str) -> bool | None: ...
    def fact_of(self, text: str) -> ValueFact: ...


class Executes(Protocol[A_contra]):
    def execute(self, action: A_contra, snapshot: Snapshot) -> ActionResult: ...


def _not_sent(verb: str, code: ErrorCode, detail: str) -> ActionResult:
    """Nothing was sent: v1's `not_delivered` with its code and a closed detail."""
    return ActionResult(verb, Delivery.NOT_DELIVERED, code=code, evidence={"detail": detail})


def _refused(verb: str, code: ErrorCode, detail: str) -> ActResult:
    """Nothing was sent: v1's `not_delivered` with its code and a closed detail."""
    return ActResult(
        ActionResult(verb, Delivery.NOT_DELIVERED, code=code, evidence={"detail": detail}), None
    )


class ActPort:
    """`Ports.act` for one task."""

    def __init__(
        self,
        *,
        world: Reads,
        semantic: Executes[Action],
        physical: Executes[PhysicalAction],
        interrupted: Callable[[], str | None],
    ) -> None:
        self._world = world
        self._semantic = semantic
        self._physical = physical
        self._interrupted = interrupted

    def act(self, dispatch: Dispatch) -> ActResult:
        verb = dispatch.action.verb
        token = self._interrupted()
        if token is not None:  # nothing is sent after the key ([V2.40] Q4)
            raise PortInterrupted(token)
        if verb == "wait":
            # the watch after a wait IS the wait (slice 5, change-aware): nothing is actuated
            return ActResult(ActionResult(verb, Delivery.DELIVERED_VERIFIED), None)
        if verb == "scroll":
            return _refused(verb, ErrorCode.GATE_BLOCKED, "not_in_step")
        found = self._resolve(dispatch)
        if found is None:
            return _refused(verb, ErrorCode.ELEMENT_NOT_FOUND, "target_gone")
        element, snapshot = found
        if (element.role, normalise_name(element.name)) != (
            dispatch.role,
            normalise_name(dispatch.name),
        ):
            return _refused(verb, ErrorCode.ELEMENT_NOT_FOUND, "identity_changed")
        result = self._send(dispatch, element, snapshot)
        if result.delivery is Delivery.NOT_DELIVERED and not result.skipped:
            return ActResult(result, None)
        return ActResult(result, self._read_back(dispatch, element))

    def _resolve(self, dispatch: Dispatch) -> tuple[Element, Snapshot] | None:
        """The one element of the last observation with the dispatch's RuntimeId."""
        last = self._world.last
        if last is None or not dispatch.runtime_id:
            return None
        matches = [e for e in last.snapshot.elements if e.runtime_id == dispatch.runtime_id]
        if len(matches) != 1:
            return None
        return matches[0], last.snapshot

    def _send(self, dispatch: Dispatch, element: Element, snapshot: Snapshot) -> ActionResult:
        verb = dispatch.action.verb
        if verb in SEMANTIC_VERBS:
            return self._semantic_send(dispatch, element, snapshot)
        if verb in PHYSICAL_VERBS:
            return self._physical_send(dispatch, element, snapshot)
        return _not_sent(verb, ErrorCode.GATE_BLOCKED, "not_in_step")

    def _semantic_send(
        self, dispatch: Dispatch, element: Element, snapshot: Snapshot
    ) -> ActionResult:
        verb = dispatch.action.verb
        option = dispatch.action.option if verb == "choose" else None
        result = self._semantic.execute(Action(verb, element.id, option=option), snapshot)
        nothing_sent = result.delivery is Delivery.NOT_DELIVERED and not result.skipped
        if verb in FALLBACK_VERBS and nothing_sent:  # provably nothing sent ([V2.40] Q6)
            return self._physical.execute(PhysicalAction("click", element.id), snapshot)
        return result

    def _physical_send(
        self, dispatch: Dispatch, element: Element, snapshot: Snapshot
    ) -> ActionResult:
        verb, key = dispatch.action.verb, dispatch.action.key
        if verb == "type":
            if dispatch.text is None:  # the runner always resolves it (RT-36); fail closed
                return _not_sent(verb, ErrorCode.INVALID_TEXT, "text_missing")
            sent = PhysicalAction("type", element.id, text=dispatch.text)
        elif verb == "press":
            if key is None:  # validate refuses this shape first; fail closed
                return _not_sent(verb, ErrorCode.GATE_BLOCKED, "key_missing")
            sent = PhysicalAction("press", element.id, chord=key)
        else:  # focus: a hit-tested click, as in v1
            sent = PhysicalAction("click", element.id)
        return self._physical.execute(sent, snapshot)

    def _read_back(self, dispatch: Dispatch, element: Element) -> bool | None:
        """[V2.11] item 2, [V2.24] item 3: True verified, False a mismatch, None unread; None for
        a verb with no read-back."""
        verb = dispatch.action.verb
        if verb in WANTED:
            return self._world.read_state(element, WANTED[verb])
        if verb == "type" and dispatch.text is not None:
            return _equal(self._world.read_value(element), self._world.fact_of(dispatch.text))
        if verb == "choose" and dispatch.action.option is not None:
            return _equal(
                self._world.read_value(element), self._world.fact_of(dispatch.action.option)
            )
        return None


def _equal(read: ValueFact | None, wanted: ValueFact) -> bool | None:
    return None if read is None else read == wanted
