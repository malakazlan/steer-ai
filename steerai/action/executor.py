"""The semantic executor (design 5.9 [v1.1]): verbs through UIA patterns on the snapshot's live
handles. Admission and failure classification are shared with the physical executor
(`steerai.action.execution`).

Every verb reads its own state back (`poll_state`): equal before the call means already in state
and nothing is sent; settled on a different state is `verify_failed`; never settled is
`verify_timeout`. `set_value` replaces the value, never appends; newlines are literal content.
`check`/`uncheck` toggle at most `MAX_TOGGLES` times because three-state controls cycle; a
toggle or a `select` that settles back where it began is `unchanged` (T1 decides). `choose`
expands a ComboBox, selects the one option of that name in its own subtree and collapses it again
on every path after Expand (widget-verbs W4). Physical input (click at a point, keystrokes,
chords) lives in the physical executor, not here.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.execution import (
    UIA_EXPAND_COLLAPSE_PATTERN,
    UIA_INVOKE_PATTERN,
    UIA_SCROLL_ITEM_PATTERN,
    UIA_SELECTION_ITEM_PATTERN,
    UIA_TOGGLE_PATTERN,
    UIA_VALUE_PATTERN,
    Admission,
    Call,
    LiveWindows,
    RefusalError,
    UiaBindings,
    WindowState,
    classify,
)
from steerai.action.keyboard import GateApproval
from steerai.action.options import OptionItem, OptionSource, clean_option, option_key
from steerai.action.readback import Readback, poll_found, poll_state, poll_until
from steerai.action.sanitize import normalise_newlines, sanitize_value
from steerai.errors import ErrorCode, SteerError
from steerai.world.elements import Element
from steerai.world.model import Snapshot, normalise_name

VERBS: frozenset[str] = frozenset(
    {
        "click",
        "focus",
        "set_value",
        "check",
        "uncheck",
        "expand",
        "collapse",
        "select",
        "choose",
        "scroll_into_view",
    }
)
_TEXT_VERBS = frozenset({"set_value"})
_OPTION_VERBS = frozenset({"choose"})
_POSTCONDITION_VERBS = frozenset({"click", "scroll_into_view"})
MAX_TOGGLES = 2
_TOGGLE_OFF, _TOGGLE_ON = 0, 1
_COLLAPSED, _EXPANDED, _LEAF_NODE = 0, 1, 3
OPTION_POLL_S = 0.01
OPTION_DEADLINE_S = 1.0
OPTION_MAX_READS = 100
"""The option poll after Expand (W4-3, WV-36): every 10 ms, capped at 1 s and at 100 reads.
Measured (probe `widget_patterns --section choose`, Chrome 156, n=10): the option appears in
the ComboBox's subtree 16.8-34.1 ms after Expand."""
COLLAPSE_POLL_S = 0.01
COLLAPSE_DEADLINE_S = 1.0
COLLAPSE_MAX_READS = 100
"""The Collapse readback (W4 v1 verdict ruling (b)): measured, it still reads Expanded at once
10/10 and Collapsed within 10.6-25.0 ms, so it is polled, capped at 1 s and 100 reads."""


@dataclass(frozen=True, slots=True)
class Action:
    verb: str
    element_id: int
    text: str | None = field(default=None, repr=False)
    approval: GateApproval | None = None
    """The gate's typed approval for credential entry into a secure field (5.13)."""
    postcondition: Callable[[], bool] | None = None
    """Caller-supplied readback for `click` and `scroll_into_view`."""
    option: str | None = field(default=None, repr=False)
    """The option a `choose` picks, folded by `clean_option`: screen text, never in a repr or
    in the result's evidence."""

    def __post_init__(self) -> None:
        if self.verb not in VERBS:
            raise ValueError(f"unknown verb {self.verb!r}")
        if self.verb in _TEXT_VERBS and self.text is None:
            raise ValueError(f"{self.verb} needs text")
        if self.verb not in _TEXT_VERBS and self.text is not None:
            raise ValueError(f"{self.verb} takes no text")
        if self.verb in _OPTION_VERBS:
            if not isinstance(self.option, str):
                raise ValueError(f"{self.verb} needs an option")
            object.__setattr__(self, "option", clean_option(self.option))
        elif self.option is not None:
            raise ValueError(f"{self.verb} takes no option")
        if self.approval is not None and not isinstance(self.approval, GateApproval):
            raise TypeError("approval must be a GateApproval from the gate, not a flag")
        if self.postcondition is not None and self.verb not in _POSTCONDITION_VERBS:
            raise ValueError(f"{self.verb} reads its own state back; it takes no postcondition")


class Executor:
    def __init__(
        self,
        *,
        uia: UiaBindings | None = None,
        windows: WindowState | None = None,
        clock: Callable[[], float] = time.time,
        blocker: Callable[[], str | None] | None = None,
        options: OptionSource | None = None,
    ) -> None:
        self._uia = UiaBindings.load() if uia is None else uia
        self._blocker = blocker
        self._options = options
        """Reads a ComboBox's options for `choose`; without one, `choose` is refused."""
        self._admission = Admission(
            windows=LiveWindows() if windows is None else windows, bindings=self._uia, clock=clock
        )

    def execute(self, action: Action, snapshot: Snapshot) -> ActionResult:
        call = Call(action.verb, "semantic", blocker=self._blocker)
        try:
            element, handle = self._admission.admit(action, snapshot)
            link = None
            if action.verb == "click" and not handle.GetCurrentPattern(UIA_INVOKE_PATTERN):
                link = row_link(snapshot, element)
            if link is not None:
                # a row with no Invoke acts through its one link of the same name (#124)
                action = replace(action, element_id=link.id)
                element, handle = self._admission.admit(action, snapshot)
            call.secure = element.secure
            done = _DISPATCH[action.verb](self, action, handle, call)
            if link is None:
                return done
            return replace(done, evidence={**done.evidence, "acted_on": "row_link"})
        except Exception as exc:
            result = classify(call, exc)
            if result is None:
                raise
            return result

    # ---- verbs -------------------------------------------------------------------------------

    def _click(self, action: Action, handle: Any, call: Call) -> ActionResult:
        pattern = self._uia.pattern(handle, UIA_INVOKE_PATTERN, "InvokePattern")
        call.send("InvokePattern.Invoke", pattern.Invoke)
        if action.postcondition is None:
            return _unverified(call, "no_postcondition")
        return _finish(call, _poll_postcondition(action.postcondition))

    def _focus(self, _action: Action, handle: Any, call: Call) -> ActionResult:
        """UIA `SetFocus`, read back through `HasKeyboardFocus`: the fallback when a
        focus click is refused for a transparent overlay (5.9 [v1.7])."""
        call.send("SetFocus", handle.SetFocus)
        return _finish(call, poll_state(lambda: bool(handle.CurrentHasKeyboardFocus), True))

    def _set_value(self, action: Action, handle: Any, call: Call) -> ActionResult:
        if action.text is None:  # enforced by Action.__post_init__; kept for the type checker
            raise SteerError(ErrorCode.INVALID_TEXT, "set_value needs text")
        wanted = sanitize_value(action.text)
        pattern = self._uia.pattern(handle, UIA_VALUE_PATTERN, "ValuePattern")

        def read() -> str:
            raw = pattern.CurrentValue
            return "" if raw is None else normalise_newlines(str(raw))

        if read() == wanted:
            return ActionResult.already_satisfied(action.verb)
        if bool(pattern.CurrentIsReadOnly):
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "read_only")
        call.send("ValuePattern.SetValue", lambda: pattern.SetValue(wanted))
        evidence: dict[str, Any] = {}
        if action.approval is not None:
            evidence["decision_id"] = action.approval.decision_id
        if not call.secure:
            evidence["chars"] = len(wanted)
        return _finish(call, poll_state(read, wanted), evidence)

    def _toggle_to(self, action: Action, handle: Any, call: Call, wanted: int) -> ActionResult:
        pattern = self._uia.pattern(handle, UIA_TOGGLE_PATTERN, "TogglePattern")

        last: list[int] = []

        def read() -> int:
            last.append(int(pattern.CurrentToggleState))
            return last[-1]

        initial = read()
        if initial == wanted:
            return ActionResult.already_satisfied(action.verb)
        toggles = 0
        while True:
            call.send("TogglePattern.Toggle", pattern.Toggle)
            toggles += 1
            readback = poll_state(read, wanted)
            if readback.verified or not readback.settled or toggles >= MAX_TOGGLES:
                return _finish(call, readback, {"toggles": toggles})
            if last[-1] == initial:
                # a server-confirmed box keeps its old value until it answers (WV-03): a second
                # Toggle would double-flip it, so T1 decides within its deadline (W2 verdict 2)
                return ActionResult(
                    verb=call.verb,
                    delivery=Delivery.DELIVERED_UNVERIFIED,
                    steps=call.steps,
                    code=ErrorCode.VERIFY_TIMEOUT,
                    evidence={"toggles": toggles, "detail": "unchanged"},
                )

    def _check(self, action: Action, handle: Any, call: Call) -> ActionResult:
        return self._toggle_to(action, handle, call, _TOGGLE_ON)

    def _uncheck(self, action: Action, handle: Any, call: Call) -> ActionResult:
        return self._toggle_to(action, handle, call, _TOGGLE_OFF)

    def _expand_to(self, action: Action, handle: Any, call: Call, wanted: int) -> ActionResult:
        pattern = self._uia.pattern(handle, UIA_EXPAND_COLLAPSE_PATTERN, "ExpandCollapsePattern")

        def read() -> int:
            return int(pattern.CurrentExpandCollapseState)

        state = read()
        if state == wanted:
            return ActionResult.already_satisfied(action.verb)
        if state == _LEAF_NODE:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "leaf_node")
        if wanted == _EXPANDED:
            call.send("ExpandCollapsePattern.Expand", pattern.Expand)
        else:
            call.send("ExpandCollapsePattern.Collapse", pattern.Collapse)
        return _finish(call, poll_state(read, wanted))

    def _expand(self, action: Action, handle: Any, call: Call) -> ActionResult:
        return self._expand_to(action, handle, call, _EXPANDED)

    def _collapse(self, action: Action, handle: Any, call: Call) -> ActionResult:
        return self._expand_to(action, handle, call, _COLLAPSED)

    def _select(self, action: Action, handle: Any, call: Call) -> ActionResult:
        pattern = self._uia.pattern(handle, UIA_SELECTION_ITEM_PATTERN, "SelectionItemPattern")

        def read() -> bool:
            return bool(pattern.CurrentIsSelected)

        if read():
            return ActionResult.already_satisfied(action.verb)
        call.send("SelectionItemPattern.Select", pattern.Select)
        readback = poll_state(read, True)
        if readback.verified or not readback.settled:
            return _finish(call, readback)
        # aria-selected may be set asynchronously: never a second Select, T1 decides within its
        # deadline (W3 verdict ruling (b), as a toggle settling on its initial value)
        return ActionResult(
            verb=call.verb,
            delivery=Delivery.DELIVERED_UNVERIFIED,
            steps=call.steps,
            code=ErrorCode.VERIFY_TIMEOUT,
            evidence={"detail": "unchanged"},
        )

    def _choose(self, action: Action, handle: Any, call: Call) -> ActionResult:
        """W4: refusals before Expand send nothing; after Expand the ComboBox is collapsed again
        on every path, except when it does not read Expanded (the ARIA shape) or the blocker
        fired; a refusal is `not_delivered` only once the polled Collapse read back Collapsed
        (W4 v2 verdict). Every pattern call goes through `call.send`; a COM error is classified
        by the phase the call log is in. Every comparison is `option_key` (case folded): options
        that differ only in case are `ambiguous_option`. Accepted residual (PR #192 review): when
        two options differ only in case and the Value already equals one of them, the choose
        reads already satisfied before Expand, so it never learns the pair is ambiguous."""
        if action.option is None:  # enforced by Action.__post_init__; kept for the type checker
            raise SteerError(ErrorCode.INVALID_TEXT, "choose needs an option")
        option = option_key(action.option)
        expander = self._uia.pattern(handle, UIA_EXPAND_COLLAPSE_PATTERN, "ExpandCollapsePattern")
        state = int(expander.CurrentExpandCollapseState)
        if state == _LEAF_NODE:
            # a datalist input reads LeafNode; IsReadOnly and an Edit child do not tell (W4 v2)
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "editable_combobox")
        value = self._uia.pattern(handle, UIA_VALUE_PATTERN, "ValuePattern")

        def read_value() -> str:
            raw = value.CurrentValue
            return option_key("" if raw is None else str(raw))

        if read_value() == option:
            return ActionResult.already_satisfied(action.verb)
        options = self._options
        if options is None:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "no_option_source")
        try:
            if state != _EXPANDED:
                call.send("ExpandCollapsePattern.Expand", expander.Expand)
            found = poll_found(
                lambda: _named(options.list_items(handle), option),
                step_s=OPTION_POLL_S,
                deadline_s=OPTION_DEADLINE_S,
                max_reads=OPTION_MAX_READS,
            )
            refusal = _option_refusal(found)
            if refusal is None and found is not None:
                item = self._uia.pattern(
                    found[0].handle, UIA_SELECTION_ITEM_PATTERN, "SelectionItemPattern"
                )
                call.send("SelectionItemPattern.Select", item.Select)
            elif int(expander.CurrentExpandCollapseState) != _EXPANDED:
                # the box does not read Expanded: no Collapse, it throws there (W4 v2)
                detail = "no_options" if found is None else str(refusal)
                return _choose_result(call, detail, code=ErrorCode.ELEMENT_NOT_ACTIONABLE)
            if not _collapsed(expander, call):
                return _choose_result(call, "left_expanded", cause=refusal)
        except RefusalError as exc:
            if exc.code is not ErrorCode.GATE_BLOCKED or call.sent is None:
                raise
            # the person took over after Expand: no Select, no Collapse (W4 v1 verdict item 3)
            return _choose_result(
                call, "left_expanded", code=ErrorCode.GATE_BLOCKED, blocker=exc.detail
            )
        return _chosen(call, refusal, read_value, option)

    def _scroll_into_view(self, action: Action, handle: Any, call: Call) -> ActionResult:
        pattern = self._uia.pattern(handle, UIA_SCROLL_ITEM_PATTERN, "ScrollItemPattern")

        def on_screen() -> bool:
            return not bool(handle.CurrentIsOffscreen)

        if action.postcondition is None and on_screen():
            return ActionResult.already_satisfied(action.verb)
        call.send("ScrollItemPattern.ScrollIntoView", pattern.ScrollIntoView)
        if action.postcondition is None:
            return _finish(call, poll_state(on_screen, True))
        return _finish(call, _poll_postcondition(action.postcondition))


_ACTING_ROLES = frozenset(
    {
        "Hyperlink",
        "Button",
        "SplitButton",
        "MenuItem",
        "CheckBox",
        "RadioButton",
        "TabItem",
        "ComboBox",
        "Edit",
    }
)
"""Roles a row may hold that act on their own: more than the one link means the row is not
just a link (a "Delete chat" button inside it), so the click stays refused."""


_FALLBACK_ROLES = frozenset({"Button", "SplitButton", "Hyperlink", "MenuItem", "TabItem"})
"""Plain controls a physical click may stand in for when they expose no InvokePattern (#149)."""


def _under(snapshot: Snapshot, ancestor: Element) -> list[Element]:
    by_id = {e.id: e for e in snapshot.elements}

    def below(element: Element) -> bool:
        parent = element.parent_id
        while parent is not None:
            if parent == ancestor.id:
                return True
            parent = by_id[parent].parent_id if parent in by_id else None
        return False

    return [e for e in snapshot.elements if below(e)]


def physical_click_target(snapshot: Snapshot, element: Element) -> bool:
    """Whether a physical click may stand in for a refused Invoke (5.9 [v2.50], #149): a plain
    acting control with no acting descendant, since the physical hit test accepts any
    descendant and a row would press the Delete button it holds (#84/#124)."""
    return element.role in _FALLBACK_ROLES and not any(
        e.role in _ACTING_ROLES for e in _under(snapshot, element)
    )


def row_link(snapshot: Snapshot, row: Element) -> Element | None:
    """The row's one acting descendant when it is a Hyperlink with the row's exact name and no
    automation id: the gate judged the row's name and id, so the link carries nothing it did
    not judge (#84). Anything else is None: the click stays refused."""
    acting = [e for e in _under(snapshot, row) if e.role in _ACTING_ROLES]
    if len(acting) != 1 or acting[0].role != "Hyperlink":
        return None
    link = acting[0]
    if link.automation_id or normalise_name(link.name) != normalise_name(row.name):
        return None
    return link


_DISPATCH: dict[str, Callable[[Executor, Action, Any, Call], ActionResult]] = {
    "click": Executor._click,
    "focus": Executor._focus,
    "set_value": Executor._set_value,
    "check": Executor._check,
    "uncheck": Executor._uncheck,
    "expand": Executor._expand,
    "collapse": Executor._collapse,
    "select": Executor._select,
    "choose": Executor._choose,
    "scroll_into_view": Executor._scroll_into_view,
}


def _named(items: Sequence[OptionItem], option: str) -> list[OptionItem] | None:
    """The ListItems whose `option_key` is the option's (W4-05); None while there are none."""
    found = [item for item in items if option_key(item.name) == option]
    return found or None


def _option_refusal(found: list[OptionItem] | None) -> str | None:
    """Why the options found cannot be chosen, or None for exactly one enabled, selectable
    ListItem (WV-39, WV-40)."""
    if found is None:
        return "no_match"
    if len(found) > 1:
        return "ambiguous_option"
    if not found[0].enabled:
        return "option_disabled"
    if not found[0].selectable:
        return "option_unselectable"
    return None


def _collapsed(expander: Any, call: Call) -> bool:
    """Collapse unless the box already reads Collapsed, then poll the readback: measured, it
    reads Expanded at once and Collapsed within 25 ms (W4 v1). `expander` is the COM pattern."""

    def state() -> int:
        return int(expander.CurrentExpandCollapseState)

    if state() != _COLLAPSED:
        call.send("ExpandCollapsePattern.Collapse", expander.Collapse)
    return poll_state(
        state,
        _COLLAPSED,
        step_s=COLLAPSE_POLL_S,
        deadline_s=COLLAPSE_DEADLINE_S,
        max_steps=COLLAPSE_MAX_READS,
    ).verified


def _chosen(
    call: Call, refusal: str | None, read_value: Callable[[], str], option: str
) -> ActionResult:
    """After a confirmed Collapse: a refusal is `not_delivered` (the box read Collapsed again,
    W4 v2 verdict); a Select is read back through the Value, and a miss is `unchanged` for T1
    to decide, never a second Select (W4-3)."""
    if refusal is not None:
        return ActionResult(
            verb=call.verb,
            delivery=Delivery.NOT_DELIVERED,
            steps=call.steps,
            code=(
                ErrorCode.ELEMENT_NOT_FOUND
                if refusal == "no_match"
                else ErrorCode.ELEMENT_NOT_ACTIONABLE
            ),
            evidence={"detail": refusal, "expanded": True},
        )
    readback = poll_state(read_value, option)
    if readback.verified:
        return _finish(call, readback)
    return ActionResult(
        verb=call.verb,
        delivery=Delivery.DELIVERED_UNVERIFIED,
        steps=call.steps,
        code=ErrorCode.VERIFY_TIMEOUT,
        evidence={"detail": "unchanged"},
    )


def _choose_result(
    call: Call,
    detail: str,
    *,
    code: ErrorCode = ErrorCode.VERIFY_FAILED,
    cause: str | None = None,
    blocker: str | None = None,
) -> ActionResult:
    """A choose that may have changed the page and is unverified: never retry-safe once
    anything was sent (W4 v2 verdict). Evidence holds closed tokens only."""
    evidence: dict[str, Any] = {"detail": detail}
    if cause is not None:
        evidence["cause"] = cause
    if blocker is not None:
        evidence["blocker"] = blocker
    delivery = Delivery.DELIVERY_UNCERTAIN if call.sent is not None else Delivery.NOT_DELIVERED
    return ActionResult(
        verb=call.verb, delivery=delivery, steps=call.steps, code=code, evidence=evidence
    )


def _poll_postcondition(postcondition: Callable[[], bool]) -> Readback:
    """A caller predicate that stays false is not a definite wrong state: never `settled`."""
    return Readback(verified=poll_until(postcondition), settled=False)


def _finish(call: Call, readback: Readback, evidence: dict[str, Any] | None = None) -> ActionResult:
    return ActionResult(
        verb=call.verb,
        delivery=readback.delivery,
        steps=call.steps,
        code=readback.code,
        evidence=evidence or {},
    )


def _unverified(call: Call, reason: str) -> ActionResult:
    return ActionResult(
        verb=call.verb,
        delivery=Delivery.DELIVERED_UNVERIFIED,
        steps=call.steps,
        code=ErrorCode.VERIFY_TIMEOUT,
        evidence={"reason": reason},
    )
