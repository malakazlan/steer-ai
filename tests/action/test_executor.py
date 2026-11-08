"""The executor (design 5.9 [v1.1]): semantic verbs through live handles, pre-call checks, phase-
tracked failure classification, readback and dispositions.

Pure tests drive the executor with fake COM handles that expose the pattern surface the executor
uses; the Windows tests run the same verbs against a throwaway Notepad.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

import steerai.action.readback as readback_mod
from steerai.action.dispositions import Delivery
from steerai.action.execution import SNAPSHOT_MAX_AGE_S, UiaBindings
from steerai.action.executor import Action, Executor
from steerai.errors import ErrorCode, SteerError
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel
from tests.action.fakes import (
    APPROVAL,
    BOX,
    E_ACCESSDENIED,
    RPC_S_SERVER_UNAVAILABLE,
    TYPE_IDS,
    UIA_E_ELEMENTNOTAVAILABLE,
    UIA_E_ELEMENTNOTENABLED,
    UIA_E_TIMEOUT,
    UIA_EXPAND,
    UIA_INVOKE,
    UIA_SCROLL_ITEM,
    UIA_SELECTION_ITEM,
    UIA_TOGGLE,
    UIA_VALUE,
    FakeHandle,
    FakeWindows,
    com_error,
)


def _executor(windows: FakeWindows | None = None, *, now: float = 0.0) -> Executor:
    return Executor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=windows or FakeWindows(),
        clock=lambda: now,
    )


def _snapshot(
    handle: FakeHandle,
    *,
    role: str = "Button",
    name: str = "Send",
    stable: bool = True,
    enabled: bool = True,
    secure: bool = False,
    handles: list[Any] | None = None,
) -> Snapshot:
    handle.control_type = TYPE_IDS[role]
    handle.name = name
    element = Element(
        id=1,
        depth=0,
        role=role,
        name=name,
        box=BOX,
        runtime_id=(7, 1),
        enabled=enabled,
        secure=secure,
    )
    return Snapshot(
        id="s1",
        hwnd=0x10,
        process="test.exe",
        title="t",
        taken_at=0.0,
        elements=[element],
        node_count=1,
        stable=stable,
        handles=[handle] if handles is None else handles,
    )


@pytest.fixture
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    monkeypatch.setattr(
        readback_mod, "time", SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep)
    )


# ---- verbs ------------------------------------------------------------------------------------


def test_click_invokes_and_is_unverified_without_a_postcondition(fast_clock: None) -> None:
    handle = FakeHandle()
    result = _executor().execute(Action(verb="click", element_id=1), _snapshot(handle))
    assert handle.invoked == 1
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.evidence == {"reason": "no_postcondition"}
    assert result.ok is False
    assert result.steps[0].label == "InvokePattern.Invoke"
    assert result.steps[0].mechanism == "semantic"


def test_click_with_a_postcondition_is_verified(fast_clock: None) -> None:
    handle = FakeHandle()
    action = Action(verb="click", element_id=1, postcondition=lambda: handle.invoked == 1)
    result = _executor().execute(action, _snapshot(handle))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.ok is True


def test_a_postcondition_that_already_holds_refuses_the_action(fast_clock: None) -> None:
    handle = FakeHandle()
    action = Action(verb="click", element_id=1, postcondition=lambda: True)
    result = _executor().execute(action, _snapshot(handle))
    assert handle.calls == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence == {
        "reason": "verify_failed",
        "detail": "precondition_already_satisfied",
    }


def test_set_value_is_verified_by_reading_the_value_back(fast_clock: None) -> None:
    handle = FakeHandle()
    action = Action(verb="set_value", element_id=1, text="hello")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.value == "hello"
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.ok is True
    assert result.evidence == {"chars": 5}


def test_set_value_already_equal_sends_nothing(fast_clock: None) -> None:
    handle = FakeHandle(value="hello")
    action = Action(verb="set_value", element_id=1, text="hello")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.calls == []
    assert result.ok is True
    assert result.delivery is Delivery.NOT_DELIVERED


def test_set_value_on_a_read_only_field_is_not_actionable(fast_clock: None) -> None:
    handle = FakeHandle(read_only=True)
    action = Action(verb="set_value", element_id=1, text="hello")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.delivery is Delivery.NOT_DELIVERED
    assert handle.calls == []


def test_set_value_on_a_read_only_field_already_equal_is_already_in_state(
    fast_clock: None,
) -> None:
    handle = FakeHandle(read_only=True, value="hello")
    action = Action(verb="set_value", element_id=1, text="hello")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert result.skipped is True
    assert result.ok is True


def test_set_value_rejects_invalid_text_before_touching_the_element(fast_clock: None) -> None:
    handle = FakeHandle()
    action = Action(verb="set_value", element_id=1, text="bad\x1b")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert result.code is ErrorCode.INVALID_TEXT
    assert result.retry_safe is True
    assert handle.calls == []
    # the sanitizer's message names the code point; evidence carries the code only
    assert result.evidence == {"reason": "invalid_text"}


def test_set_value_passes_newlines_as_literal_content(fast_clock: None) -> None:
    handle = FakeHandle()
    action = Action(verb="set_value", element_id=1, text="line one\r\nline two")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.value == "line one\nline two"
    assert result.delivery is Delivery.DELIVERED_VERIFIED


def test_set_value_readback_ignores_the_line_ending_the_app_stores(fast_clock: None) -> None:
    @dataclass
    class _CrLfEdit(FakeHandle):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
            inner = super().GetCurrentPattern(pattern_id)
            handle = self

            class _Pattern:
                def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                    return self

                @property
                def CurrentIsReadOnly(self) -> bool:  # noqa: N802
                    return False

                @property
                def CurrentValue(self) -> str | None:  # noqa: N802
                    return None if handle.value is None else handle.value.replace("\n", "\r\n")

                def SetValue(self, text: str) -> None:  # noqa: N802
                    inner.SetValue(text)

            return _Pattern()

    handle = _CrLfEdit()
    action = Action(verb="set_value", element_id=1, text="a\nb")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.calls == ["set:a\nb"]
    assert result.delivery is Delivery.DELIVERED_VERIFIED


def test_set_value_with_empty_text_clears_the_field(fast_clock: None) -> None:
    handle = FakeHandle(value="old")
    result = _executor().execute(
        Action(verb="set_value", element_id=1, text=""), _snapshot(handle, role="Edit")
    )
    assert handle.value == ""
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.evidence == {"chars": 0}


def test_set_value_treats_a_null_value_as_empty(fast_clock: None) -> None:
    handle = FakeHandle(value=None)
    cleared = _executor().execute(
        Action(verb="set_value", element_id=1, text=""), _snapshot(handle, role="Edit")
    )
    assert cleared.skipped is True
    assert handle.calls == []
    literal = _executor().execute(
        Action(verb="set_value", element_id=1, text="None"), _snapshot(handle, role="Edit")
    )
    assert handle.calls == ["set:None"]
    assert literal.delivery is Delivery.DELIVERED_VERIFIED


def test_set_value_that_the_app_normalises_differently_is_verify_failed(
    fast_clock: None,
) -> None:
    @dataclass
    class _TrimmingEdit(FakeHandle):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
            inner = super().GetCurrentPattern(pattern_id)
            handle = self

            class _Pattern:
                def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                    return self

                @property
                def CurrentIsReadOnly(self) -> bool:  # noqa: N802
                    return False

                @property
                def CurrentValue(self) -> str | None:  # noqa: N802
                    return handle.value

                def SetValue(self, text: str) -> None:  # noqa: N802
                    inner.SetValue(text.strip())

            return _Pattern()

    handle = _TrimmingEdit()
    result = _executor().execute(
        Action(verb="set_value", element_id=1, text="hello "), _snapshot(handle, role="Edit")
    )
    assert handle.value == "hello"
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.retry_safe is False


def test_check_is_idempotent_and_uncheck_toggles_once(fast_clock: None) -> None:
    handle = FakeHandle(toggled=1)
    checked = _executor().execute(
        Action(verb="check", element_id=1), _snapshot(handle, role="CheckBox")
    )
    assert checked.ok is True
    assert handle.calls == []
    unchecked = _executor().execute(
        Action(verb="uncheck", element_id=1), _snapshot(handle, role="CheckBox")
    )
    assert handle.calls == ["toggle"]
    assert unchecked.delivery is Delivery.DELIVERED_VERIFIED
    assert unchecked.evidence == {"toggles": 1}


def test_expand_and_select_are_verified_by_their_state(fast_clock: None) -> None:
    handle = FakeHandle(patterns={UIA_EXPAND, UIA_SELECTION_ITEM})
    expanded = _executor().execute(
        Action(verb="expand", element_id=1), _snapshot(handle, role="TreeItem")
    )
    selected = _executor().execute(
        Action(verb="select", element_id=1), _snapshot(handle, role="ListItem")
    )
    assert expanded.delivery is Delivery.DELIVERED_VERIFIED
    assert selected.delivery is Delivery.DELIVERED_VERIFIED


def test_missing_pattern_is_not_actionable(fast_clock: None) -> None:
    handle = FakeHandle(patterns=set())
    result = _executor().execute(Action(verb="click", element_id=1), _snapshot(handle))
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.delivery is Delivery.NOT_DELIVERED


def test_unknown_element_id_is_element_not_found(fast_clock: None) -> None:
    result = _executor().execute(Action(verb="click", element_id=9), _snapshot(FakeHandle()))
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.retry_safe is True


def test_unknown_verb_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown verb"):
        Action(verb="teleport", element_id=1)


def test_set_value_without_text_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="text"):
        Action(verb="set_value", element_id=1)


@pytest.mark.parametrize("verb", ["click", "check", "uncheck", "expand", "collapse", "select"])
def test_text_on_a_verb_that_takes_none_is_rejected_at_construction(verb: str) -> None:
    with pytest.raises(ValueError, match="text"):
        Action(verb=verb, element_id=1, text="stray")


def test_action_repr_and_result_never_contain_the_typed_text(fast_clock: None) -> None:
    handle = FakeHandle()
    typed = "typed-value-42"
    action = Action(verb="set_value", element_id=1, text=typed)
    assert typed not in repr(action)
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert typed not in str(result.to_dict())


# ---- pre-call checks (SX-06, SX-11, SX-12, SX-13) ----------------------------------------------


def test_disabled_element_in_the_snapshot_is_refused_before_any_call(fast_clock: None) -> None:
    handle = FakeHandle()
    result = _executor().execute(
        Action(verb="click", element_id=1), _snapshot(handle, enabled=False)
    )
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.delivery is Delivery.NOT_DELIVERED
    assert handle.calls == []
    assert result.evidence == {"reason": "element_not_actionable", "detail": "disabled"}


def test_unstable_snapshot_is_refused(fast_clock: None) -> None:
    handle = FakeHandle()
    result = _executor().execute(
        Action(verb="click", element_id=1), _snapshot(handle, stable=False)
    )
    assert result.code is ErrorCode.UNSTABLE_UI
    assert handle.calls == []
    assert result.evidence["detail"] == "snapshot_unstable"


def test_snapshot_older_than_the_cap_is_refused(fast_clock: None) -> None:
    handle = FakeHandle()
    stale = _executor(now=SNAPSHOT_MAX_AGE_S + 0.001).execute(
        Action(verb="click", element_id=1), _snapshot(handle)
    )
    assert stale.code is ErrorCode.UNSTABLE_UI
    assert stale.evidence["detail"] == "snapshot_stale"
    assert handle.calls == []
    fresh = _executor(now=SNAPSHOT_MAX_AGE_S).execute(
        Action(verb="click", element_id=1), _snapshot(handle)
    )
    assert fresh.delivery is Delivery.DELIVERED_UNVERIFIED


@pytest.mark.parametrize(
    ("windows", "code"),
    [
        (FakeWindows(alive=False), ErrorCode.APP_CRASHED),
        (FakeWindows(cloaked=True), ErrorCode.WINDOW_OTHER_DESKTOP),
        (FakeWindows(cloaked=None), ErrorCode.WINDOW_OTHER_DESKTOP),  # unreadable (#48)
        (FakeWindows(enabled=False), ErrorCode.INTERRUPT_MODAL),
    ],
)
def test_window_is_rechecked_before_the_call(
    fast_clock: None, windows: FakeWindows, code: ErrorCode
) -> None:
    handle = FakeHandle()
    result = _executor(windows).execute(Action(verb="click", element_id=1), _snapshot(handle))
    assert result.code is code
    assert result.delivery is Delivery.NOT_DELIVERED
    assert handle.calls == []


def test_handle_whose_name_changed_since_the_snapshot_is_not_found(fast_clock: None) -> None:
    handle = FakeHandle()
    snapshot = _snapshot(handle, name="Delete row 3")
    handle.name = "Delete row 4"
    result = _executor().execute(Action(verb="click", element_id=1), snapshot)
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert handle.calls == []
    assert result.evidence == {"reason": "element_not_found", "detail": "identity_changed"}


def test_handle_whose_control_type_changed_is_not_found(fast_clock: None) -> None:
    handle = FakeHandle()
    snapshot = _snapshot(handle)
    handle.control_type = TYPE_IDS["Edit"]
    result = _executor().execute(Action(verb="click", element_id=1), snapshot)
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert handle.calls == []


def test_identity_check_normalises_whitespace_and_compatibility_forms(fast_clock: None) -> None:
    handle = FakeHandle()
    snapshot = _snapshot(handle, name="Send  message")
    handle.name = "Send\u00a0message"
    result = _executor().execute(Action(verb="click", element_id=1), snapshot)
    assert handle.invoked == 1
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED


def test_identity_check_tolerates_a_null_name(fast_clock: None) -> None:
    handle = FakeHandle()
    snapshot = _snapshot(handle, name="")
    handle.name = None  # type: ignore[assignment]  # VT_EMPTY from the provider
    _executor().execute(Action(verb="click", element_id=1), snapshot)
    assert handle.invoked == 1


# ---- secure fields (SX-05) ----------------------------------------------------------------------


def test_secure_element_is_refused_without_gate_approval(fast_clock: None) -> None:
    handle = FakeHandle()
    snapshot = _snapshot(handle, role="Edit", name="Password", secure=True)
    refused = _executor().execute(Action(verb="set_value", element_id=1, text="s3cret"), snapshot)
    assert refused.code is ErrorCode.GATE_BLOCKED
    assert refused.delivery is Delivery.NOT_DELIVERED
    assert handle.calls == []
    assert "s3cret" not in str(refused.to_dict())


def test_secure_element_with_gate_approval_records_the_decision_not_the_length(
    fast_clock: None,
) -> None:
    handle = FakeHandle()
    snapshot = _snapshot(handle, role="Edit", name="Password", secure=True)
    action = Action(verb="set_value", element_id=1, text="s3cret", approval=APPROVAL)
    allowed = _executor().execute(action, snapshot)
    assert allowed.delivery is Delivery.DELIVERED_VERIFIED
    assert handle.value == "s3cret"
    assert allowed.evidence == {"decision_id": "d-7"}
    assert "s3cret" not in str(allowed.to_dict())


def test_approval_must_be_a_gate_approval_not_a_flag() -> None:
    with pytest.raises(TypeError, match="GateApproval"):
        Action(verb="set_value", element_id=1, text="x", approval=True)  # type: ignore[arg-type]


# ---- failure classification by phase (SX-01, SX-02, SX-03) ------------------------------------


def _handle_raising(error: Exception) -> FakeHandle:
    class _Broken(FakeHandle):
        def GetCurrentPattern(self, _pattern_id: int) -> Any:  # noqa: N802
            raise error

    return _Broken()


def test_steer_error_before_the_call_is_not_delivered_with_its_code(fast_clock: None) -> None:
    handle = _handle_raising(SteerError(ErrorCode.APP_CRASHED, "gone"))
    result = _executor().execute(Action(verb="click", element_id=1), _snapshot(handle))
    assert result.code is ErrorCode.APP_CRASHED
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.steps == []


@pytest.mark.parametrize(
    ("hresult", "code"),
    [
        (UIA_E_ELEMENTNOTAVAILABLE, ErrorCode.ELEMENT_NOT_FOUND),
        (E_ACCESSDENIED, ErrorCode.ELEMENT_NOT_FOUND),
        (UIA_E_ELEMENTNOTENABLED, ErrorCode.ELEMENT_NOT_ACTIONABLE),
        (RPC_S_SERVER_UNAVAILABLE, ErrorCode.APP_CRASHED),
        (UIA_E_TIMEOUT, ErrorCode.PERCEPTION_TIMEOUT),
        (0x8000FFFF, ErrorCode.PERCEPTION_TIMEOUT),
    ],
)
def test_com_error_before_the_call_maps_hresult_to_a_code(
    fast_clock: None, hresult: int, code: ErrorCode
) -> None:
    handle = _handle_raising(com_error(hresult))
    result = _executor().execute(Action(verb="click", element_id=1), _snapshot(handle))
    assert result.code is code
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.retry_safe is True
    assert result.evidence == {"reason": code.value, "hresult": f"0x{hresult:08X}"}


def test_os_error_before_the_call_is_perception_timeout(fast_clock: None) -> None:
    handle = _handle_raising(OSError("UIA client gone"))
    result = _executor().execute(Action(verb="click", element_id=1), _snapshot(handle))
    assert result.code is ErrorCode.PERCEPTION_TIMEOUT
    assert result.delivery is Delivery.NOT_DELIVERED


def test_programming_error_before_the_call_propagates(fast_clock: None) -> None:
    handle = _handle_raising(TypeError("bug"))
    with pytest.raises(TypeError, match="bug"):
        _executor().execute(Action(verb="click", element_id=1), _snapshot(handle))


class _ValueReadFails(FakeHandle):
    """CurrentValue raises `error` once SetValue has been sent: the element went away mid-poll."""

    def __init__(self, error: Exception, *, fail_call: bool = False) -> None:
        super().__init__(patterns={UIA_VALUE})
        self.error = error
        self.fail_call = fail_call

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
        inner = super().GetCurrentPattern(pattern_id)
        handle = self

        class _Pattern:
            def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                return self

            @property
            def CurrentIsReadOnly(self) -> bool:  # noqa: N802
                return False

            @property
            def CurrentValue(self) -> str | None:  # noqa: N802
                if handle.calls:
                    raise handle.error
                value: str | None = inner.CurrentValue
                return value

            def SetValue(self, text: str) -> None:  # noqa: N802
                inner.SetValue(text)
                if handle.fail_call:
                    raise handle.error

        return _Pattern()


def test_set_value_whose_readback_raises_a_steer_error_is_uncertain(fast_clock: None) -> None:
    handle = _ValueReadFails(SteerError(ErrorCode.ELEMENT_NOT_FOUND, "element vanished"))
    action = Action(verb="set_value", element_id=1, text="hello")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.calls == ["set:hello"], "SetValue was sent before the readback failed"
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.retry_safe is False
    assert result.ok is False
    assert result.steps[0].label == "ValuePattern.SetValue"
    assert result.steps[0].outcome == "succeeded"
    assert result.evidence == {"reason": "element_not_found", "phase": "readback"}


def test_set_value_whose_readback_raises_a_com_error_is_uncertain(fast_clock: None) -> None:
    handle = _ValueReadFails(com_error(UIA_E_ELEMENTNOTAVAILABLE))
    action = Action(verb="set_value", element_id=1, text="hello")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.calls == ["set:hello"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence == {
        "reason": "element_not_found",
        "phase": "readback",
        "hresult": f"0x{UIA_E_ELEMENTNOTAVAILABLE:08X}",
    }


def test_set_value_whose_call_raises_a_com_error_is_uncertain(fast_clock: None) -> None:
    handle = _ValueReadFails(com_error(RPC_S_SERVER_UNAVAILABLE), fail_call=True)
    action = Action(verb="set_value", element_id=1, text="hello")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.steps[0].label == "ValuePattern.SetValue"
    assert result.steps[0].outcome == "failed"
    assert result.evidence["phase"] == "call"
    assert result.evidence["reason"] == "app_crashed"


def test_click_whose_postcondition_raises_after_invoke_is_uncertain(fast_clock: None) -> None:
    handle = FakeHandle()

    def postcondition() -> bool:
        if handle.invoked:
            raise SteerError(ErrorCode.ELEMENT_NOT_FOUND, "target gone after invoke")
        return False

    result = _executor().execute(
        Action(verb="click", element_id=1, postcondition=postcondition), _snapshot(handle)
    )
    assert handle.invoked == 1, "Invoke was sent before the postcondition raised"
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.retry_safe is False


def test_programming_error_after_the_call_is_uncertain_not_lost(fast_clock: None) -> None:
    handle = FakeHandle()

    def postcondition() -> bool:
        if handle.invoked:
            raise TypeError("bug in the postcondition")
        return False

    result = _executor().execute(
        Action(verb="click", element_id=1, postcondition=postcondition), _snapshot(handle)
    )
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence == {"reason": "verify_failed", "phase": "readback", "error": "TypeError"}


# ---- state verbs: tri-state, expand states, scroll (SX-08, SX-20, SX-21, SX-23) -----------------


class _ThreeState(FakeHandle):
    """A tri-state checkbox: Toggle cycles Off(0) -> On(1) -> Indeterminate(2) -> Off(0)."""

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
        if pattern_id != UIA_TOGGLE:
            return None
        handle = self

        class _Pattern:
            def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                return self

            def Toggle(self) -> None:  # noqa: N802
                handle.toggled = (handle.toggled + 1) % 3
                handle.calls.append("toggle")

            @property
            def CurrentToggleState(self) -> int:  # noqa: N802
                return handle.toggled

        return _Pattern()


def test_check_on_an_indeterminate_checkbox_toggles_twice_until_on(fast_clock: None) -> None:
    handle = _ThreeState(toggled=2)
    result = _executor().execute(
        Action(verb="check", element_id=1), _snapshot(handle, role="CheckBox")
    )
    assert handle.calls == ["toggle", "toggle"]
    assert handle.toggled == 1
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert len(result.steps) == 2
    assert result.evidence == {"toggles": 2}


def test_uncheck_on_an_indeterminate_checkbox_lands_on_off(fast_clock: None) -> None:
    handle = _ThreeState(toggled=2)
    result = _executor().execute(
        Action(verb="uncheck", element_id=1), _snapshot(handle, role="CheckBox")
    )
    assert handle.calls == ["toggle"]
    assert handle.toggled == 0
    assert result.delivery is Delivery.DELIVERED_VERIFIED


def test_toggle_that_never_reaches_the_wanted_state_stops_after_two_and_fails(
    fast_clock: None,
) -> None:
    class _Stuck(_ThreeState):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
            pattern = super().GetCurrentPattern(pattern_id)
            handle = self

            def toggle() -> None:
                handle.toggled = 2 if handle.toggled == 0 else 0
                handle.calls.append("toggle")

            pattern.Toggle = toggle
            return pattern

    handle = _Stuck(toggled=0)
    result = _executor().execute(
        Action(verb="check", element_id=1), _snapshot(handle, role="CheckBox")
    )
    assert handle.calls == ["toggle", "toggle"]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_FAILED


def test_toggle_whose_readback_never_settles_is_not_toggled_again(fast_clock: None) -> None:
    class _Flickering(FakeHandle):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
            if pattern_id != UIA_TOGGLE:
                return None
            handle = self
            reads = iter([0] + [0, 2] * 20)

            class _Pattern:
                def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                    return self

                def Toggle(self) -> None:  # noqa: N802
                    handle.calls.append("toggle")

                @property
                def CurrentToggleState(self) -> int:  # noqa: N802
                    return next(reads)

            return _Pattern()

    handle = _Flickering()
    result = _executor().execute(
        Action(verb="check", element_id=1), _snapshot(handle, role="CheckBox")
    )
    assert handle.calls == ["toggle"]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_TIMEOUT


def test_expand_on_a_leaf_node_is_refused_before_any_call(fast_clock: None) -> None:
    handle = FakeHandle(patterns={UIA_EXPAND}, expanded=3)
    result = _executor().execute(
        Action(verb="expand", element_id=1), _snapshot(handle, role="TreeItem")
    )
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert handle.calls == []
    assert result.evidence["detail"] == "leaf_node"


def test_collapse_on_a_partially_expanded_node_sends_and_verifies(fast_clock: None) -> None:
    handle = FakeHandle(patterns={UIA_EXPAND}, expanded=2)
    result = _executor().execute(
        Action(verb="collapse", element_id=1), _snapshot(handle, role="TreeItem")
    )
    assert handle.calls == ["collapse"]
    assert result.delivery is Delivery.DELIVERED_VERIFIED


def test_collapse_is_verified_and_idempotent(fast_clock: None) -> None:
    handle = FakeHandle(patterns={UIA_EXPAND}, expanded=1)
    collapsed = _executor().execute(
        Action(verb="collapse", element_id=1), _snapshot(handle, role="TreeItem")
    )
    assert handle.expanded == 0
    assert collapsed.steps[0].label == "ExpandCollapsePattern.Collapse"
    assert collapsed.delivery is Delivery.DELIVERED_VERIFIED
    again = _executor().execute(
        Action(verb="collapse", element_id=1), _snapshot(handle, role="TreeItem")
    )
    assert again.skipped is True
    assert again.ok is True


def test_select_that_the_app_rejects_is_verify_failed(fast_clock: None) -> None:
    class _Rejecting(FakeHandle):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
            pattern = super().GetCurrentPattern(pattern_id)
            handle = self

            def select() -> None:
                handle.calls.append("select")

            pattern.Select = select
            return pattern

    handle = _Rejecting(patterns={UIA_SELECTION_ITEM})
    result = _executor().execute(
        Action(verb="select", element_id=1), _snapshot(handle, role="ListItem")
    )
    assert handle.calls == ["select"]
    assert result.code is ErrorCode.VERIFY_FAILED


def test_scroll_into_view_reads_back_on_screen_without_a_postcondition(fast_clock: None) -> None:
    handle = FakeHandle(patterns={UIA_SCROLL_ITEM}, offscreen=True)
    result = _executor().execute(
        Action(verb="scroll_into_view", element_id=1), _snapshot(handle, role="ListItem")
    )
    assert handle.calls == ["scroll"]
    assert result.steps[0].label == "ScrollItemPattern.ScrollIntoView"
    assert result.delivery is Delivery.DELIVERED_VERIFIED


def test_scroll_into_view_that_stays_offscreen_is_verify_failed(fast_clock: None) -> None:
    class _Stuck(FakeHandle):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
            pattern = super().GetCurrentPattern(pattern_id)
            pattern.ScrollIntoView = lambda: self.calls.append("scroll")
            return pattern

    handle = _Stuck(patterns={UIA_SCROLL_ITEM}, offscreen=True)
    result = _executor().execute(
        Action(verb="scroll_into_view", element_id=1), _snapshot(handle, role="ListItem")
    )
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_FAILED


def test_scroll_into_view_already_on_screen_is_already_in_state(fast_clock: None) -> None:
    handle = FakeHandle(patterns={UIA_SCROLL_ITEM}, offscreen=False)
    result = _executor().execute(
        Action(verb="scroll_into_view", element_id=1), _snapshot(handle, role="ListItem")
    )
    assert handle.calls == []
    assert result.skipped is True


def test_scroll_into_view_with_a_postcondition_uses_it(fast_clock: None) -> None:
    handle = FakeHandle(patterns={UIA_SCROLL_ITEM}, offscreen=True)
    result = _executor().execute(
        Action(verb="scroll_into_view", element_id=1, postcondition=lambda: bool(handle.calls)),
        _snapshot(handle, role="ListItem"),
    )
    assert result.delivery is Delivery.DELIVERED_VERIFIED


# ---- other boundaries ---------------------------------------------------------------------------


class _RawPattern:
    """A pattern object with no QueryInterface (what a non-comtypes shim or test double exposes)."""

    def __init__(self, handle: FakeHandle) -> None:
        self._handle = handle

    def Invoke(self) -> None:  # noqa: N802
        self._handle.invoked += 1


class _NoQueryInterface(FakeHandle):
    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
        return _RawPattern(self) if pattern_id == UIA_INVOKE else None


def test_pattern_without_query_interface_is_used_as_is(fast_clock: None) -> None:
    handle = _NoQueryInterface()
    result = _executor().execute(Action(verb="click", element_id=1), _snapshot(handle))
    assert handle.invoked == 1
    assert result.steps[0].label == "InvokePattern.Invoke"


@dataclass
class _InertValue(FakeHandle):
    """SetValue is accepted but the control never changes its value."""

    reads: int = 0

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
        if pattern_id != UIA_VALUE:
            return None
        handle = self

        class _Pattern:
            def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                return self

            @property
            def CurrentIsReadOnly(self) -> bool:  # noqa: N802
                return False

            @property
            def CurrentValue(self) -> str:  # noqa: N802
                handle.reads += 1
                return ""

            def SetValue(self, text: str) -> None:  # noqa: N802
                handle.calls.append(f"set:{len(text)}")

        return _Pattern()


def test_set_value_that_silently_does_nothing_is_verify_failed_and_not_retry_safe(
    fast_clock: None,
) -> None:
    handle = _InertValue()
    action = Action(verb="set_value", element_id=1, text="hello")
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.calls == ["set:5"]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.ok is False
    assert result.retry_safe is False
    # one equality read before SetValue, eight polls, one final read at the deadline
    assert handle.reads == 1 + 8 + 1


@pytest.mark.parametrize(
    ("current", "wanted"),
    [("hello ", "hello"), (" hello", "hello"), ("hello  world", "hello world"), ("Hello", "hello")],
)
def test_set_value_compares_exactly_not_after_whitespace_or_case_folding(
    fast_clock: None, current: str, wanted: str
) -> None:
    handle = FakeHandle(value=current)
    action = Action(verb="set_value", element_id=1, text=wanted)
    result = _executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.calls == [f"set:{wanted}"]
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.skipped is False


def test_snapshot_without_live_handles_is_element_not_found(fast_clock: None) -> None:
    result = _executor().execute(
        Action(verb="click", element_id=1), _snapshot(FakeHandle(), handles=[])
    )
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.retry_safe is True


def test_bindings_from_a_module_missing_an_interface_fail_at_construction() -> None:
    module = SimpleNamespace(
        UIA_ButtonControlTypeId=50000,
        IUIAutomationInvokePattern=object(),
        IUIAutomationValuePattern=object(),
    )
    with pytest.raises(OSError, match="IUIAutomationExpandCollapsePattern"):
        UiaBindings.from_module(module)


def test_bindings_from_a_module_map_control_types_and_interfaces() -> None:
    names = [
        "IUIAutomationInvokePattern",
        "IUIAutomationValuePattern",
        "IUIAutomationExpandCollapsePattern",
        "IUIAutomationSelectionItemPattern",
        "IUIAutomationTogglePattern",
        "IUIAutomationScrollItemPattern",
    ]
    module = SimpleNamespace(UIA_ButtonControlTypeId=50000, **{n: object() for n in names})
    bindings = UiaBindings.from_module(module)
    assert bindings.control_types == {50000: "Button"}
    assert set(bindings.interfaces) == {
        UIA_INVOKE,
        UIA_VALUE,
        UIA_EXPAND,
        UIA_SELECTION_ITEM,
        UIA_TOGGLE,
        UIA_SCROLL_ITEM,
    }


# ---- Windows -----------------------------------------------------------------------------------


def _notepad_document() -> tuple[subprocess.Popen[bytes], Snapshot, Element]:
    proc = subprocess.Popen(["notepad.exe"])  # noqa: S607 - fixed, well-known executable
    window = wait_window_for_pid(proc.pid)
    time.sleep(0.5)
    snapshot = WorldModel(UiaClient()).observe(window.hwnd, cap_s=5.0)
    target = next(e for e in snapshot.elements if e.role in ("Document", "Edit"))
    return proc, snapshot, target


@pytest.mark.windows
def test_set_value_on_notepad_document_round_trips() -> None:
    proc, snapshot, target = _notepad_document()
    try:
        action = Action(verb="set_value", element_id=target.id, text="steer wrote this")
        result = Executor().execute(action, snapshot)
        assert result.ok is True, result.to_dict()
        assert result.delivery is Delivery.DELIVERED_VERIFIED
        again = Executor().execute(action, snapshot)
        assert again.delivery is Delivery.NOT_DELIVERED
        assert again.ok is True
    finally:
        proc.terminate()


@pytest.mark.windows
def test_dead_window_is_app_crashed_with_nothing_sent() -> None:
    proc, snapshot, target = _notepad_document()
    proc.kill()
    proc.wait(timeout=5)
    time.sleep(0.2)
    result = Executor().execute(
        Action(verb="set_value", element_id=target.id, text="too late"), snapshot
    )
    assert result.code is ErrorCode.APP_CRASHED, result.to_dict()
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.steps == []


@pytest.mark.windows
def test_real_com_error_from_a_dead_handle_becomes_a_result() -> None:
    proc, snapshot, target = _notepad_document()
    proc.kill()
    proc.wait(timeout=5)
    time.sleep(0.2)
    # Bypass the window re-check so the dead handle itself raises the real COMError.
    executor = Executor(windows=FakeWindows(), clock=lambda: snapshot.taken_at)
    result = executor.execute(
        Action(verb="set_value", element_id=target.id, text="too late"), snapshot
    )
    assert result.delivery is Delivery.NOT_DELIVERED, result.to_dict()
    assert result.code in {
        ErrorCode.ELEMENT_NOT_FOUND,
        ErrorCode.APP_CRASHED,
        ErrorCode.PERCEPTION_TIMEOUT,
    }
    assert result.evidence["hresult"].startswith("0x")


# ---- focus ------------------------------------------------------------------------------------


def test_focus_sets_focus_and_reads_keyboard_focus_back(fast_clock: Any) -> None:
    handle = FakeHandle()
    result = _executor().execute(Action(verb="focus", element_id=1), _snapshot(handle, role="Edit"))
    assert result.ok is True
    assert handle.calls == ["SetFocus"]
    assert result.steps[-1].label == "SetFocus"


def test_focus_that_never_takes_is_verify_failed(fast_clock: Any) -> None:
    """Focus that settles elsewhere is a settled wrong state (5.9 [v1.1]), not a timeout."""
    handle = FakeHandle()
    handle.SetFocus = lambda: handle.calls.append("SetFocus")  # type: ignore[method-assign]
    result = _executor().execute(Action(verb="focus", element_id=1), _snapshot(handle, role="Edit"))
    assert result.ok is False
    assert result.code is ErrorCode.VERIFY_FAILED


def test_a_toggle_that_settles_on_the_initial_value_is_never_sent_twice(fast_clock: None) -> None:
    """A server-confirmed box keeps its old value until the server answers (WV-03, H3 n1): a
    second Toggle would double-flip it. One Toggle, unverified, and T1 decides (W2 verdict 2)."""

    class _Slow(FakeHandle):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
            if pattern_id != UIA_TOGGLE:
                return None
            handle = self

            class _Pattern:
                def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                    return self

                def Toggle(self) -> None:  # noqa: N802
                    handle.calls.append("toggle")

                @property
                def CurrentToggleState(self) -> int:  # noqa: N802
                    return handle.toggled

            return _Pattern()

    handle = _Slow(toggled=0)
    result = _executor().execute(
        Action(verb="check", element_id=1), _snapshot(handle, role="CheckBox")
    )
    assert handle.calls == ["toggle"]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.evidence == {"toggles": 1, "detail": "unchanged"}
