"""The production live reader the runner wires into the session (RN-22)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from steerai.action.execution import UIA_VALUE_PATTERN
from steerai.action.live_reads import UiaLiveReads
from steerai.verify.t1 import LiveReads


@dataclass
class _Pattern:
    CurrentValue: str | None


@dataclass
class _Bindings:
    values: dict[int, str | None]
    asked: list[tuple[int, int, str]] = field(default_factory=list)

    def pattern(self, handle: Any, pattern_id: int, name: str) -> _Pattern:
        self.asked.append((handle, pattern_id, name))
        return _Pattern(self.values[handle])


@dataclass
class _Client:
    focused: tuple[int, ...] = (42, 7)

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self.focused


def test_value_reads_the_value_pattern_of_the_live_handle() -> None:
    bindings = _Bindings({1: "hello", 2: None})
    live: LiveReads = UiaLiveReads(_Client(), bindings)
    assert live.value(1) == "hello"
    assert live.value(2) is None
    assert bindings.asked == [
        (1, UIA_VALUE_PATTERN, "ValuePattern"),
        (2, UIA_VALUE_PATTERN, "ValuePattern"),
    ]


def test_focus_comes_from_the_client() -> None:
    live = UiaLiveReads(_Client(focused=(42, 9)), _Bindings({}))
    assert live.focused_runtime_id() == (42, 9)
