"""The semantic executor (design 5.9): verbs through UIA patterns on the snapshot's live handles.

Every verb has a fixed postcondition template read back after the call (5.10 [v0.3]); idempotent
verbs (`check`, `uncheck`, `expand`, `collapse`, `select`) send nothing when the target is already
in the wanted state. `set_value` follows the type-step resume rule [v1.0]: equal → already in
state; otherwise the value is replaced, never appended. Physical input (click at a point, typed
keystrokes, chords) lives in the physical executor, not here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from steerai.action.dispositions import ActionResult, Delivery, Step
from steerai.action.readback import poll_until, readback_delivery
from steerai.action.sanitize import NewlineBinding, sanitize_text
from steerai.errors import ErrorCode, SteerError
from steerai.world.model import Snapshot

Verb = Literal[
    "click", "set_value", "check", "uncheck", "expand", "collapse", "select", "scroll_into_view"
]
VERBS: frozenset[str] = frozenset(
    {"click", "set_value", "check", "uncheck", "expand", "collapse", "select", "scroll_into_view"}
)

# UIA pattern ids (UIAutomationClient constants), kept as literals so the executor is testable
# without comtypes; the generated module exposes the same values.
UIA_INVOKE_PATTERN = 10000
UIA_VALUE_PATTERN = 10002
UIA_EXPAND_COLLAPSE_PATTERN = 10005
UIA_SELECTION_ITEM_PATTERN = 10010
UIA_TOGGLE_PATTERN = 10015
UIA_SCROLL_ITEM_PATTERN = 10017
_TOGGLE_ON = 1
_EXPANDED = 1
_COLLAPSED = 0


@dataclass(frozen=True, slots=True)
class Action:
    verb: str
    element_id: int
    text: str | None = None
    newline: NewlineBinding | None = None
    allow_secure: bool = False
    postcondition: Callable[[], bool] | None = None
    """Caller-supplied readback for verbs without an intrinsic one (click)."""

    def __post_init__(self) -> None:
        if self.verb not in VERBS:
            raise ValueError(f"unknown verb {self.verb!r}")
        if self.verb == "set_value" and self.text is None:
            raise ValueError("set_value needs text")


class Executor:
    def execute(self, action: Action, snapshot: Snapshot) -> ActionResult:
        try:
            element = snapshot.element(action.element_id)
            handle = snapshot.handle(action.element_id)
            if element.secure and not action.allow_secure:
                raise SteerError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE,
                    f"element {element.id} is a secure field; credential entry needs gate approval",
                )
            return _DISPATCH[action.verb](self, action, handle)
        except SteerError as exc:
            return _refused(action.verb, exc, uncertain=exc.code is ErrorCode.APP_CRASHED)

    # ---- verbs -------------------------------------------------------------------------------

    def _click(self, action: Action, handle: Any) -> ActionResult:
        pattern = _pattern(handle, UIA_INVOKE_PATTERN, "InvokePattern")
        pattern.Invoke()
        step = Step(label="InvokePattern.Invoke", mechanism="semantic", outcome="succeeded")
        if action.postcondition is None:
            return _delivered(action.verb, [step], verified=None)
        return _delivered(action.verb, [step], verified=poll_until(action.postcondition))

    def _set_value(self, action: Action, handle: Any) -> ActionResult:
        if action.text is None:  # enforced by Action.__post_init__; kept for the type checker
            raise SteerError(ErrorCode.INVALID_TEXT, "set_value needs text")
        parts = sanitize_text(action.text, newline=action.newline)
        if any(kind == "key" for kind, _ in parts):
            raise SteerError(
                ErrorCode.INVALID_TEXT,
                "set_value cannot type key chords; use the physical executor",
            )
        wanted = "".join(value for _, value in parts)
        pattern = _pattern(handle, UIA_VALUE_PATTERN, "ValuePattern")
        if bool(pattern.CurrentIsReadOnly):
            raise SteerError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "value is read-only")
        if str(pattern.CurrentValue) == wanted:
            return ActionResult.already_satisfied(action.verb)
        pattern.SetValue(wanted)
        step = Step(label="ValuePattern.SetValue", mechanism="semantic", outcome="succeeded")
        verified = poll_until(lambda: str(pattern.CurrentValue) == wanted)
        return _delivered(action.verb, [step], verified=verified, evidence={"chars": len(wanted)})

    def _toggle_to(self, action: Action, handle: Any, wanted: int) -> ActionResult:
        pattern = _pattern(handle, UIA_TOGGLE_PATTERN, "TogglePattern")
        if int(pattern.CurrentToggleState) == wanted:
            return ActionResult.already_satisfied(action.verb)
        pattern.Toggle()
        step = Step(label="TogglePattern.Toggle", mechanism="semantic", outcome="succeeded")
        verified = poll_until(lambda: int(pattern.CurrentToggleState) == wanted)
        return _delivered(action.verb, [step], verified=verified)

    def _check(self, action: Action, handle: Any) -> ActionResult:
        return self._toggle_to(action, handle, _TOGGLE_ON)

    def _uncheck(self, action: Action, handle: Any) -> ActionResult:
        return self._toggle_to(action, handle, 1 - _TOGGLE_ON)

    def _expand_to(self, action: Action, handle: Any, wanted: int) -> ActionResult:
        pattern = _pattern(handle, UIA_EXPAND_COLLAPSE_PATTERN, "ExpandCollapsePattern")
        if int(pattern.CurrentExpandCollapseState) == wanted:
            return ActionResult.already_satisfied(action.verb)
        if wanted == _EXPANDED:
            pattern.Expand()
        else:
            pattern.Collapse()
        label = (
            "ExpandCollapsePattern.Expand"
            if wanted == _EXPANDED
            else "ExpandCollapsePattern.Collapse"
        )
        step = Step(label=label, mechanism="semantic", outcome="succeeded")
        verified = poll_until(lambda: int(pattern.CurrentExpandCollapseState) == wanted)
        return _delivered(action.verb, [step], verified=verified)

    def _expand(self, action: Action, handle: Any) -> ActionResult:
        return self._expand_to(action, handle, _EXPANDED)

    def _collapse(self, action: Action, handle: Any) -> ActionResult:
        return self._expand_to(action, handle, _COLLAPSED)

    def _select(self, action: Action, handle: Any) -> ActionResult:
        pattern = _pattern(handle, UIA_SELECTION_ITEM_PATTERN, "SelectionItemPattern")
        if bool(pattern.CurrentIsSelected):
            return ActionResult.already_satisfied(action.verb)
        pattern.Select()
        step = Step(label="SelectionItemPattern.Select", mechanism="semantic", outcome="succeeded")
        verified = poll_until(lambda: bool(pattern.CurrentIsSelected))
        return _delivered(action.verb, [step], verified=verified)

    def _scroll_into_view(self, action: Action, handle: Any) -> ActionResult:
        pattern = _pattern(handle, UIA_SCROLL_ITEM_PATTERN, "ScrollItemPattern")
        pattern.ScrollIntoView()
        step = Step(
            label="ScrollItemPattern.ScrollIntoView", mechanism="semantic", outcome="succeeded"
        )
        verified = None if action.postcondition is None else poll_until(action.postcondition)
        return _delivered(action.verb, [step], verified=verified)


_DISPATCH: dict[str, Callable[[Executor, Action, Any], ActionResult]] = {
    "click": Executor._click,
    "set_value": Executor._set_value,
    "check": Executor._check,
    "uncheck": Executor._uncheck,
    "expand": Executor._expand,
    "collapse": Executor._collapse,
    "select": Executor._select,
    "scroll_into_view": Executor._scroll_into_view,
}


def _pattern(handle: Any, pattern_id: int, name: str) -> Any:
    # COM pattern objects have no stubs; the executor reads only the documented members.
    raw = handle.GetCurrentPattern(pattern_id)
    if not raw:
        raise SteerError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"element exposes no {name}")
    query = getattr(raw, "QueryInterface", None)
    return raw if query is None else query(_interface_for(pattern_id))


def _interface_for(pattern_id: int) -> Any:
    """The comtypes interface class for a pattern id, or None when comtypes is absent (tests)."""
    try:
        from comtypes.gen import UIAutomationClient  # noqa: PLC0415 - Windows only
    except ImportError:
        return None
    return {
        UIA_INVOKE_PATTERN: UIAutomationClient.IUIAutomationInvokePattern,
        UIA_VALUE_PATTERN: UIAutomationClient.IUIAutomationValuePattern,
        UIA_EXPAND_COLLAPSE_PATTERN: UIAutomationClient.IUIAutomationExpandCollapsePattern,
        UIA_SELECTION_ITEM_PATTERN: UIAutomationClient.IUIAutomationSelectionItemPattern,
        UIA_TOGGLE_PATTERN: UIAutomationClient.IUIAutomationTogglePattern,
        UIA_SCROLL_ITEM_PATTERN: UIAutomationClient.IUIAutomationScrollItemPattern,
    }[pattern_id]


def _delivered(
    verb: str,
    steps: list[Step],
    *,
    verified: bool | None,
    evidence: dict[str, Any] | None = None,
) -> ActionResult:
    delivery = readback_delivery(verified=verified)
    code = None if delivery is Delivery.DELIVERED_VERIFIED else ErrorCode.VERIFY_TIMEOUT
    return ActionResult(
        verb=verb, delivery=delivery, steps=steps, code=code, evidence=evidence or {}
    )


def _refused(verb: str, exc: SteerError, *, uncertain: bool) -> ActionResult:
    delivery = Delivery.DELIVERY_UNCERTAIN if uncertain else Delivery.NOT_DELIVERED
    return ActionResult(
        verb=verb,
        delivery=delivery,
        steps=[Step(label=exc.code.value, mechanism="none", outcome="failed")],
        code=exc.code,
        evidence={"reason": exc.message},
    )
