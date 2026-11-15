"""The act port (adapter slice 6; packet 2.5, section 3, rev 3.8 "Slice 6 design", rev 3.9 the
edge-case fold-in and [V2.51]).

`ActPort.act(dispatch)` sends one action through v1's executors, over injected collaborators so it
runs and is tested off Windows: the world ports (the last observation handed out, a fresh look and
the live reads), a semantic executor (`action.executor.Executor`, built in slice 7 over
`act_guards.ToggleGuard`), a physical one (`action.physical.PhysicalExecutor`, built over
`act_guards.InputGuard` with `blocker = interrupted`) and the desk (`act_guards.GuardDesk`).

The order of every act:
1. the switch, read once more before anything is sent; after it, the port never raises:
   any error becomes `delivery_uncertain` ([V2.51] (2)), never `not_delivered`, so no retry
   follows a crash that may have come after keys went out;
2. nothing to send: a `wait` actuates nothing (the change-aware watch after it IS the wait);
   `scroll`, `open` and `switch` are refused (behind `validate`, [V2.41] (2));
3. the dispatch, checked again: R0/R1 only (AP-13), text only on a type and an option only on a
   choose (AP-26), focus only on a field ([V2.51] (1)), a key only from the closed map (AP-12),
   Backspace only on Steer's own text ([V2.51] (5));
4. before anything that sends (every verb but wait), v1's `_boundary`: the idle rule (the person
   idle `IDLE_S`, v1's constant, UNMEASURED with no probe yet; waited in kill-aware steps up to
   `MAX_WAIT_S` or the wall budget left), then, when it waited, a fresh look: the same window
   handle and title, or nothing is sent (AP-03, AP-30, v1 `_fallback_look`). Before physical
   input (type, press, focus, the fallback click) v1's tap refusal comes first;
5. resolve on the last observation by RuntimeId (exactly one element), and check it is the
   element the gate rated (`Dispatch.role` / `Dispatch.name`, RT-04); keys never go to a
   listed, unread or live secure field (AP-08, AP-09); a type and a press go only where the live
   focus is the target itself (a ComboBox: or its own Edit), and the guard is armed on exactly
   that set (AP-10, AP-11);
6. the executor for the verb; v1's `Admission` re-checks the live element; a check or uncheck
   whose second Toggle never went (`ToggleGuard`, v1's `unchanged`, or a read that failed after
   the first) is one delivered Toggle, `second_toggle_refused`, and one the guard's re-read found
   in the wanted state is verified (`settled_on_reread`, [V2.52] (a));
7. a click whose semantic call was refused for `no_InvokePattern` with nothing tried falls back
   to v1's physical click, exactly as v1 does (AP-04..06, 5.9 [v2.50]). A tree item's
   expand or collapse by click (v1's `expand_by_click`, packet §3) is deferred (AP-25, #353):
   refused `expand_needs_click`, nothing sent;
8. the read-back of a read-back verb, attached only when something was sent; v1's delivery is
   kept as reported beside it ([V2.51] (2)).
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Final, Protocol, TypeVar

from steerai.action.dispositions import ActionResult, Delivery, Step
from steerai.action.execution import code_for
from steerai.action.executor import Action, Executor, aim_of, click_point, physical_click_target
from steerai.action.physical import PhysicalAction, PhysicalExecutor
from steerai.errors import ErrorCode, SteerError
from steerai.loop2.act_guards import (
    KEY_CHORDS,
    SECOND_REFUSED,
    SETTLED,
    InputGuard,
    ToggleGuard,
)
from steerai.loop2.actuate import key_targets
from steerai.loop2.loop import ActResult, Dispatch, PortInterrupted
from steerai.loop2.world_ports import KILL_POLL_S, kill_sleep
from steerai.orchestrator.loop import IDLE_S, MAX_WAIT_S, WAIT_POLL_S
from steerai.verify.predicates import FIELD_ROLES
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
NOT_IN_STEP: Final = frozenset({"scroll", "open", "switch"})
"""Verbs `validate` refuses in step 3 ([V2.41] (2)); the port refuses them again."""
STEP_RISKS: Final = frozenset({"R0", "R1"})
"""Step 3 dispatches only these (the runner ends the task on a commit first, AP-13)."""
WANTED: Final[Mapping[str, str]] = {
    "check": "on",
    "uncheck": "off",
    "select": "selected",
    "expand": "expanded",
    "collapse": "collapsed",
}
"""The live state each state verb reads back ([V2.29] Q2)."""
NO_INVOKE: Final = "no_InvokePattern"
"""v1's refusal detail for a control with no InvokePattern: the only one that falls back."""
FOCUS_READ_S: Final = 0.5
"""How long the focus read-back polls for the focus to arrive after the click, in `KILL_POLL_S`
steps. UNMEASURED (no probe yet); the watch after the act reads the focus again."""
EXPAND_BY_CLICK: Final = "expand_by_click"
"""v1's refusal detail for a tree item whose expand or collapse needs a click
(`executor._tree_expand`); the port defers that click (AP-25, #353)."""


class Reads(Protocol):
    """What the act port reads from the world ports (slice 5's `WorldPorts`)."""

    last: Observed | None

    def observe(self) -> Observed: ...
    def read_value(self, element: Element) -> ValueFact | None: ...
    def read_state(self, element: Element, wanted: str) -> bool | None: ...
    def read_secure(self, element: Element) -> bool | None: ...
    def fact_of(self, text: str) -> ValueFact: ...


class Desk(Protocol):
    """What the act port reads and arms before physical input (`act_guards.GuardDesk`)."""

    def tap_refusal(self) -> str | None: ...
    def last_input(self) -> float: ...
    def takeover_token(self) -> str: ...
    def prove_hooks(self) -> None:
        """[V2.57] LW-01: both hooks see a fresh probe, or `PortInterrupted("monitor_lost")`."""
        ...

    def focused(self) -> tuple[int, ...] | None: ...
    def armed(self, focus: frozenset[tuple[int, ...]]) -> AbstractContextManager[None]: ...


class Executes(Protocol[A_contra]):
    def execute(self, action: A_contra, snapshot: Snapshot) -> ActionResult: ...


class _Refused(Exception):  # noqa: N818 - a control-flow signal: nothing was sent
    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _not_sent(verb: str, code: ErrorCode, detail: str) -> ActionResult:
    """Nothing was sent: v1's `not_delivered` with its code and a closed detail."""
    return ActionResult(verb, Delivery.NOT_DELIVERED, code=code, evidence={"detail": detail})


class ActPort:
    """`Ports.act` for one task."""

    def __init__(  # noqa: PLR0913 - every collaborator is injected so the port runs pure in tests
        self,
        *,
        world: Reads,
        semantic: Executes[Action],
        physical: Executes[PhysicalAction],
        desk: Desk,
        interrupted: Callable[[], str | None],
        clock: Callable[[], float],
        wall_left: Callable[[], float],
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        _refuse_unguarded(semantic, physical)
        self._world = world
        self._semantic = semantic
        self._physical = physical
        self._desk = desk
        self._interrupted = interrupted
        self._clock = clock
        self._wall_left = wall_left
        self._pause = pause
        self._sent: ActionResult | None = None
        """The last result an executor returned in this act: its steps survive a later error."""

    def act(self, dispatch: Dispatch) -> ActResult:
        verb = dispatch.action.verb
        self._sent = None
        token = self._interrupted()
        if token is not None:  # nothing is sent after the key ([V2.40] Q4)
            raise PortInterrupted(token)
        try:
            return self._act(dispatch)
        except PortInterrupted:
            raise  # raised only by the waits before anything is sent
        except _Refused as refused:
            return ActResult(_not_sent(verb, refused.code, refused.detail), None)
        except Exception as exc:  # noqa: BLE001 - [V2.51] (2): fail closed, never raise
            code, _extra = code_for(exc)  # AP-27: a raw COMError keeps its taxonomy code
            evidence = {"detail": "port_error", "error": type(exc).__name__}
            if code is not None:
                evidence["code"] = code.value
            steps: list[Step] = [] if self._sent is None else list(self._sent.steps)  # v1 sent
            failed = ActionResult(
                verb,
                Delivery.DELIVERY_UNCERTAIN,
                steps=steps,
                code=ErrorCode.VERIFY_FAILED,
                evidence=evidence,
            )
            return ActResult(failed, None)

    def _act(self, dispatch: Dispatch) -> ActResult:
        verb = dispatch.action.verb
        if verb == "wait":
            # the watch after a wait IS the wait (slice 5, change-aware): nothing is actuated
            return ActResult(ActionResult(verb, Delivery.DELIVERED_VERIFIED), None)
        _check_dispatch(dispatch)
        self._boundary(physical=verb in PHYSICAL_VERBS)
        element, snapshot = self._resolve(dispatch)
        if verb in SEMANTIC_VERBS:
            result = self._semantic_send(dispatch, element, snapshot)
        else:
            result = self._physical_send(dispatch, element, snapshot)
        if result.delivery is Delivery.NOT_DELIVERED and not result.skipped:
            return ActResult(result, None)
        if verb == "focus":
            focused = self._read_focus(element, snapshot)
            return ActResult(dataclasses.replace(result, verb="focus"), focused)
        return ActResult(result, self._read_back(dispatch, element))

    # ---- before anything is sent -------------------------------------------------------------

    def _boundary(self, *, physical: bool) -> None:
        """v1's `_boundary` before anything that sends: for physical input the tap refusal
        first, then the idle rule, then a fresh look when it waited (AP-03, AP-30); a refusal
        raises `_Refused`, a kill `PortInterrupted`."""
        if physical:
            tap = self._desk.tap_refusal()
            if tap is not None:
                code = (
                    ErrorCode.SECURE_DESKTOP if tap == "secure_desktop" else ErrorCode.GATE_BLOCKED
                )
                raise _Refused(code, tap)
        self._desk.prove_hooks()  # [V2.57] LW-01: before each send's idle decision
        if not self._wait_for_idle():
            return
        before = self._world.last
        fresh = self._world.observe()
        if before is None or (fresh.snapshot.hwnd, fresh.snapshot.title) != (
            before.snapshot.hwnd,
            before.snapshot.title,
        ):
            raise _Refused(ErrorCode.FOCUS_LOST, "screen_changed_during_wait")

    def _wait_for_idle(self) -> bool:
        """v1's `wait_for_idle` in kill-aware steps: True when it waited. A person busy past
        `MAX_WAIT_S` is a takeover; the wall budget left caps the wait too (`wall_budget`)."""
        started = self._clock()
        cap = min(MAX_WAIT_S, max(0.0, self._wall_left()))
        waited = False
        while True:
            token = self._interrupted()
            if token is not None:
                raise PortInterrupted(token)
            now = self._clock()
            if now - self._desk.last_input() >= IDLE_S:
                return waited
            if now - started >= cap:
                detail = self._desk.takeover_token() if cap >= MAX_WAIT_S else "wall_budget"
                raise _Refused(ErrorCode.GATE_PENDING_USER, detail)
            kill_sleep(
                min(WAIT_POLL_S, cap - (now - started)),
                interrupted=self._interrupted,
                pause=self._pause,
            )
            waited = True

    def _resolve(self, dispatch: Dispatch) -> tuple[Element, Snapshot]:
        """The one element of the last observation with the dispatch's RuntimeId, still the one
        the gate rated (RT-04); an empty RuntimeId resolves nothing (AP-14)."""
        last = self._world.last
        if last is None or not dispatch.runtime_id:
            raise _Refused(ErrorCode.ELEMENT_NOT_FOUND, "target_gone")
        matches = [e for e in last.snapshot.elements if e.runtime_id == dispatch.runtime_id]
        if len(matches) != 1:
            raise _Refused(ErrorCode.ELEMENT_NOT_FOUND, "target_gone")
        element = matches[0]
        if (element.role, normalise_name(element.name)) != (
            dispatch.role,
            normalise_name(dispatch.name),
        ):
            raise _Refused(ErrorCode.ELEMENT_NOT_FOUND, "identity_changed")
        return element, last.snapshot

    # ---- semantic ------------------------------------------------------------------------------

    def _semantic_send(
        self, dispatch: Dispatch, element: Element, snapshot: Snapshot
    ) -> ActionResult:
        verb = dispatch.action.verb
        option = dispatch.action.option if verb == "choose" else None
        result = self._executed(
            self._semantic.execute(Action(verb, element.id, option=option), snapshot)
        )
        if verb in {"check", "uncheck"}:
            result = _one_toggle(result)
        if verb in {"expand", "collapse"} and _nothing_tried(result, EXPAND_BY_CLICK):
            raise _Refused(ErrorCode.ELEMENT_NOT_ACTIONABLE, "expand_needs_click")  # AP-25
        if verb == "click" and _no_invoke(result) and physical_click_target(snapshot, element):
            return self._fallback(dispatch, result)
        return result

    def _fallback(self, dispatch: Dispatch, refused: ActionResult) -> ActionResult:
        """v1's fallback (orchestrator/loop.py `_dispatch`, 5.9 [v2.50]): the boundary, the
        fresh look when it waited, the target again, still a plain control with no acting
        descendant, then one physical click at v1's `click_point`."""
        self._boundary(physical=True)
        element, snapshot = self._resolve(dispatch)
        if not physical_click_target(snapshot, element):
            return refused  # an acting child appeared in the wait
        point = click_point(snapshot, element)
        clicked = self._executed(
            self._physical.execute(PhysicalAction("click", point.id), snapshot)
        )
        # the semantic refusal tried nothing (`_no_invoke`: no steps), so the click's steps are
        # the act's whole log; `aim` is v1's closed token for where it aimed
        evidence = {
            **clicked.evidence,
            "click_fallback": "physical",
            "semantic_detail": NO_INVOKE,
            "aim": aim_of(element, point),
        }
        return dataclasses.replace(clicked, evidence=evidence)

    def _executed(self, result: ActionResult) -> ActionResult:
        self._sent = result
        return result

    # ---- physical ------------------------------------------------------------------------------

    def _physical_send(
        self, dispatch: Dispatch, element: Element, snapshot: Snapshot
    ) -> ActionResult:
        verb = dispatch.action.verb
        if verb == "focus":  # a hit-tested click, as in v1
            return self._executed(
                self._physical.execute(PhysicalAction("click", element.id), snapshot)
            )
        _check_secure(element, snapshot, self._world.read_secure(element))
        allowed = key_targets(element, snapshot)
        if self._desk.focused() not in allowed:
            detail = "target_not_focused" if verb == "type" else "press_not_at_focus"
            raise _Refused(ErrorCode.FOCUS_LOST, detail)
        if verb == "type":
            sent = PhysicalAction("type", element.id, text=dispatch.text)
        else:
            chord = KEY_CHORDS[dispatch.action.key]  # type: ignore[index]  # checked first
            sent = PhysicalAction("press", element.id, chord=chord)
        with self._desk.armed(allowed):
            return self._executed(self._physical.execute(sent, snapshot))

    # ---- after something was sent --------------------------------------------------------------

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

    def _read_focus(self, element: Element, snapshot: Snapshot) -> bool | None:
        """[V2.51] (1): the live focus after a focus click, polled up to `FOCUS_READ_S`: True
        where keys for the target go (`key_targets`: the target, or a ComboBox's own Edit),
        False elsewhere, None unread. A kill ends the poll with what was read (the click went:
        it is never raised away)."""
        started = self._clock()
        allowed = key_targets(element, snapshot)
        while True:
            focus = self._desk.focused()
            if focus in allowed:
                return True
            if self._clock() - started >= FOCUS_READ_S or self._interrupted() is not None:
                return None if focus is None else False
            self._pause(KILL_POLL_S)


def _refuse_unguarded(semantic: object, physical: object) -> None:
    """[V2.57] LW-04: v1's executors reach the screen only guarded: a `PhysicalExecutor`'s
    input port is an `InputGuard`, an `Executor`'s UIA bindings are a `ToggleGuard`. There is
    no disarmed mode and no bypass flag; test doubles of the executors live only in the tests
    (the AST test keeps every real construction in `live_world`)."""
    if isinstance(semantic, Executor) and not isinstance(
        getattr(semantic, "_uia", None), ToggleGuard
    ):
        raise SteerError(ErrorCode.GATE_BLOCKED, "unguarded_semantic")
    unguarded = not isinstance(getattr(physical, "_inputs", None), InputGuard)
    if isinstance(physical, PhysicalExecutor) and unguarded:
        raise SteerError(ErrorCode.GATE_BLOCKED, "unguarded_physical")


def _check_dispatch(dispatch: Dispatch) -> None:
    """The dispatch, checked again before anything is sent (the runner checks it first)."""
    action = dispatch.action
    verb = action.verb
    if verb in NOT_IN_STEP:
        raise _Refused(ErrorCode.GATE_BLOCKED, "not_in_step")
    if dispatch.risk not in STEP_RISKS:
        raise _Refused(ErrorCode.GATE_BLOCKED, "commit_not_in_step")
    if (dispatch.text is not None and verb != "type") or (
        action.option is not None and verb != "choose"
    ):
        raise _Refused(ErrorCode.GATE_BLOCKED, "shape")
    if verb == "type" and dispatch.text is None:  # the runner always resolves it (RT-36)
        raise _Refused(ErrorCode.INVALID_TEXT, "text_missing")
    if verb == "focus" and dispatch.role not in FIELD_ROLES:
        raise _Refused(ErrorCode.GATE_BLOCKED, "focus_needs_field")
    if verb == "press":
        if action.key not in KEY_CHORDS:
            raise _Refused(ErrorCode.GATE_BLOCKED, "key_not_allowed")
        if action.key == "Backspace" and not dispatch.own_text:
            raise _Refused(ErrorCode.GATE_BLOCKED, "not_own_text")


def _check_secure(element: Element, snapshot: Snapshot, live: bool | None) -> None:
    """AP-08, AP-09: keys never go to a field listed secure, whose flag was not read at listing,
    or that reads secure (or unread) live now."""
    if element.secure or live is True:
        raise _Refused(ErrorCode.GATE_BLOCKED, "secure_field")
    if element.id in snapshot.unread_secure or live is None:
        raise _Refused(ErrorCode.GATE_BLOCKED, "secure_unread")


def _one_toggle(result: ActionResult) -> ActionResult:
    """[V2.52] (a): a check or uncheck whose first Toggle went and returned (its one step
    succeeded) and whose second never went. Settled on the guard's re-read: verified. Refused
    by the guard, stopped by v1 as `unchanged`, or a read after the first Toggle that failed
    (v1's uncertain, phase read-back): delivered, unverified, `second_toggle_refused`. Anything
    else is v1's result as it is."""
    one = [s.outcome for s in result.steps] == ["succeeded"]
    detail = result.evidence.get("detail")
    unread = (
        result.delivery is Delivery.DELIVERY_UNCERTAIN
        and result.evidence.get("phase") == "readback"
        and detail is None
    )
    if not one:
        return result
    if detail == SETTLED:
        evidence = {"toggles": 1, "detail": SETTLED}
        return ActionResult(
            result.verb, Delivery.DELIVERED_VERIFIED, steps=result.steps, evidence=evidence
        )
    if detail in {SECOND_REFUSED, "unchanged"} or unread:
        return ActionResult(
            result.verb,
            Delivery.DELIVERED_UNVERIFIED,
            steps=result.steps,
            code=result.code or ErrorCode.VERIFY_FAILED,  # v1's own code, kept
            evidence={"toggles": 1, "detail": SECOND_REFUSED},
        )
    return result


def _nothing_tried(result: ActionResult, detail: str) -> bool:
    """v1 refused with nothing tried, for `detail`."""
    refused = result.delivery is Delivery.NOT_DELIVERED and not result.steps
    return refused and result.evidence.get("detail") == detail


def _no_invoke(result: ActionResult) -> bool:
    """v1's fallback condition (AP-04): refused with nothing tried, for no InvokePattern."""
    return _nothing_tried(result, NO_INVOKE)


def _equal(read: ValueFact | None, wanted: ValueFact) -> bool | None:
    return None if read is None else read == wanted
