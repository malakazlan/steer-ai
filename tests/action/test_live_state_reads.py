"""The verifier's state reads (5.10 `LiveReads`): a checkbox's toggle state and an item's
selection, read live (H2 2026-11-08: `state` postconditions always read `no_pattern` because
both returned None). Probe `widget_patterns` (Chrome 156, n=8): a native checkbox exposes
Toggle and reads 1 after Toggle; a `role=tab` and a radio expose SelectionItem and read
selected after Select."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.action.execution import (
    UIA_SELECTION_ITEM_PATTERN,
    UIA_SELECTION_PATTERN,
    UIA_TOGGLE_PATTERN,
    RefusalError,
)
from steerai.action.live_reads import UiaLiveReads
from steerai.errors import ErrorCode, SteerError


class _Pattern:
    def __init__(self, toggle: int = 0, selected: bool = False) -> None:
        self.CurrentToggleState = toggle
        self.CurrentIsSelected = selected


class _Bindings:
    def __init__(self, patterns: dict[int, Any], error: Exception | None = None) -> None:
        self.patterns = patterns
        self.error = error
        self.asked: list[int] = []

    def pattern(self, _handle: Any, pattern_id: int, name: str) -> Any:
        self.asked.append(pattern_id)
        if self.error is not None:
            raise self.error
        if pattern_id not in self.patterns:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"no_{name}")
        return self.patterns[pattern_id]


class _Client:
    def focused_runtime_id(self) -> tuple[int, ...]:
        return ()


class _ComError(Exception):
    def __init__(self, hresult: int) -> None:
        super().__init__("COM failure")
        self.hresult = hresult


@pytest.mark.parametrize(("raw", "read"), [(0, 0), (1, 1), (2, 2)])
def test_the_toggle_state_is_read_through_the_toggle_pattern(raw: int, read: int) -> None:
    bindings = _Bindings({UIA_TOGGLE_PATTERN: _Pattern(toggle=raw)})
    assert UiaLiveReads(_Client(), bindings).toggle_state(object()) == read
    assert bindings.asked == [UIA_TOGGLE_PATTERN]


@pytest.mark.parametrize("selected", [True, False])
def test_the_selection_is_read_through_the_selection_item_pattern(selected: bool) -> None:
    bindings = _Bindings({UIA_SELECTION_ITEM_PATTERN: _Pattern(selected=selected)})
    assert UiaLiveReads(_Client(), bindings).is_selected(object()) is selected


def test_an_element_without_the_pattern_reads_none() -> None:
    """The verifier reads None as `no_pattern`, never as off or unselected."""
    reads = UiaLiveReads(_Client(), _Bindings({}))
    assert reads.toggle_state(object()) is None
    assert reads.is_selected(object()) is None


def test_a_com_failure_in_a_state_read_is_a_steer_error() -> None:
    reads = UiaLiveReads(_Client(), _Bindings({}, error=_ComError(-2147220991)))
    for read in (reads.toggle_state, reads.is_selected):
        with pytest.raises(SteerError) as caught:
            read(object())
        assert caught.value.message == "live_read_failed"


def test_a_programming_error_in_a_state_read_still_raises() -> None:
    reads = UiaLiveReads(_Client(), _Bindings({}, error=KeyError("bug")))
    with pytest.raises(KeyError):
        reads.toggle_state(object())


# ---- the listbox admission reads (W3b) -----------------------------------------------------------


class _Container:
    def __init__(self, multiple: bool) -> None:
        self.CurrentCanSelectMultiple = multiple


def test_has_selection_item_reads_whether_the_item_exposes_the_pattern() -> None:
    """W3b admission: a listbox option must expose SelectionItem (probe `widget_patterns
    --section disclosure`, n=3: an ARIA option and a `<select size>` option both do)."""
    assert UiaLiveReads(
        _Client(), _Bindings({UIA_SELECTION_ITEM_PATTERN: _Pattern()})
    ).has_selection_item(object())
    assert not UiaLiveReads(_Client(), _Bindings({})).has_selection_item(object())


@pytest.mark.parametrize("multiple", [True, False])
def test_the_selection_container_reads_can_select_multiple(multiple: bool) -> None:
    bindings = _Bindings({UIA_SELECTION_PATTERN: _Container(multiple)})
    assert UiaLiveReads(_Client(), bindings).selection_container(object()) is multiple
    assert bindings.asked == [UIA_SELECTION_PATTERN]


def test_a_container_without_the_selection_pattern_reads_none() -> None:
    assert UiaLiveReads(_Client(), _Bindings({})).selection_container(object()) is None


def test_a_com_failure_in_an_admission_read_is_a_steer_error() -> None:
    reads = UiaLiveReads(_Client(), _Bindings({}, error=_ComError(-2147220991)))
    for read in (reads.has_selection_item, reads.selection_container):
        with pytest.raises(SteerError) as caught:
            read(object())
        assert caught.value.message == "live_read_failed"
