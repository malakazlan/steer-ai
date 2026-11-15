"""#377: the evidence of a physical click that ended `target_moved`, for the live tree test's
failure message. Test-only: v1's executor is untouched. `RecordingInput` is v1's `LiveInput`
with the reads the click makes recorded; `record_rects` records `physical._rect`; and
`moved_evidence` names, from the calls after the click's own cursor move, which of the three
`target_moved` exits fired:
- `aim_changed`: the last look (live rectangle, visible part, aim) no longer gives the point;
- `root_window`: the root window under the point is not the task's;
- `uia_element`: the right window, but the UIA element under the point is not the target.
Coordinates, rectangles, window handles and RuntimeIds only: never a name."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.action import physical
from steerai.action.physical import LiveInput

Box = tuple[int, int, int, int]
Call = tuple[str, tuple[Any, ...], Any]  # Any: each call's own arguments and result


class RecordingInput(LiveInput):
    """v1's live input, each read the click makes appended to `calls`, in order."""

    def __init__(self, client: Any) -> None:  # Any: the UiaClient
        super().__init__(client)
        self.calls: list[Call] = []

    def move_cursor(self, x: int, y: int) -> None:
        super().move_cursor(x, y)
        self.calls.append(("move_cursor", (x, y), None))

    def cursor_pos(self) -> tuple[int, int]:
        point = super().cursor_pos()
        self.calls.append(("cursor_pos", (), point))
        return point

    def window_rect(self, hwnd: int) -> Box:
        rect = super().window_rect(hwnd)
        self.calls.append(("window_rect", (hwnd,), rect))
        return rect

    def screen_rect(self) -> Box:
        rect = super().screen_rect()
        self.calls.append(("screen_rect", (), rect))
        return rect

    def root_hwnd_at(self, x: int, y: int) -> int:
        root = super().root_hwnd_at(x, y)
        self.calls.append(("root_hwnd_at", (x, y), root))
        return root

    def runtime_id_at(self, x: int, y: int) -> tuple[int, ...]:
        hit = super().runtime_id_at(x, y)
        self.calls.append(("runtime_id_at", (x, y), hit))
        return hit


def record_rects(monkeypatch: pytest.MonkeyPatch, calls: list[Call]) -> None:
    """`physical._rect` as before, each live rectangle it reads appended to `calls`."""
    read = physical._rect

    def recorded(handle: Any) -> Box:  # Any: a UIA element
        rect = read(handle)
        calls.append(("live_rect", (), rect))
        return rect

    monkeypatch.setattr(physical, "_rect", recorded)


def _first(calls: list[Call], name: str) -> Any:  # Any: that call's result
    return next((result for n, _args, result in calls if n == name), None)


def _listed(value: Any) -> Any:  # Any: a tuple to show as a JSON list, or as it is
    return list(value) if isinstance(value, tuple) else value


def moved_evidence(calls: list[Call], hwnd: int) -> dict[str, Any]:  # Any: JSON values
    """Which `target_moved` exit fired on the last click, and what it saw."""
    moves = [i for i, (name, _args, _result) in enumerate(calls) if name == "move_cursor"]
    if not moves:
        return {"path": "no_click"}
    after = calls[moves[-1] + 1 :]
    evidence: dict[str, Any] = {  # Any: JSON values
        "path": "aim_changed",
        "clicked_at": list(calls[moves[-1]][1]),
        "cursor_at": _listed(_first(after, "cursor_pos")),
        "live_rect": _listed(_first(after, "live_rect")),
        "window_rect": _listed(_first(after, "window_rect")),
        "screen_rect": _listed(_first(after, "screen_rect")),
    }
    roots = [result for name, _args, result in after if name == "root_hwnd_at"]
    hits = [result for name, _args, result in after if name == "runtime_id_at"]
    if not roots:
        return evidence
    if roots[0] != hwnd:
        return {**evidence, "path": "root_window", "root_at_point": roots[0]}
    return {**evidence, "path": "uia_element", "hit": _listed(hits[0]) if hits else None}
