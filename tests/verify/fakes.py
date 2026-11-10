"""Synthetic snapshots for the verifier tests, shaped like the probe inbox page: a chat list,
a conversation region with message bubbles, and a composer with a Send button."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from steerai.world.elements import Element
from steerai.world.model import Snapshot

Row = tuple[int, str, str, tuple[int, ...], int | None]
"""(id, role, name, runtime_id, parent_id)."""


@dataclass
class FakeHandle:
    """What the live reads see for one element: value, toggle state, selection, expand state."""

    value: str | None = None
    toggle: int | None = None
    selected: bool | None = None
    expanded: int | None = None
    calls: list[str] = field(default_factory=list)


@dataclass
class FakeLive:
    """Stands in for the COM reads; keyed by RuntimeId so a resolved element finds its state."""

    states: dict[tuple[int, ...], FakeHandle] = field(default_factory=dict)
    focused: tuple[int, ...] = ()

    def value(self, handle: Any) -> str | None:
        return self._state(handle).value

    def toggle_state(self, handle: Any) -> int | None:
        return self._state(handle).toggle

    def is_selected(self, handle: Any) -> bool | None:
        return self._state(handle).selected

    def expand_state(self, handle: Any) -> int | None:
        return self._state(handle).expanded

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self.focused

    def _state(self, handle: Any) -> FakeHandle:
        runtime_id = tuple(handle)
        if runtime_id not in self.states:
            self.states[runtime_id] = FakeHandle()
        return self.states[runtime_id]


def build(
    rows: Sequence[Row],
    *,
    snapshot_id: str = "s1",
    title: str = "Amir Khan - Chat",
    hwnd: int = 0x10,
    stable: bool = True,
    taken_at: float = 0.0,
) -> Snapshot:
    """rows: (id, role, name, runtime_id, parent_id). Handles are the RuntimeIds (FakeLive keys)."""
    elements = []
    for element_id, role, name, runtime_id, parent_id in rows:
        depth = 0
        parent = parent_id
        by_id = {r[0]: r for r in rows}
        while parent is not None:
            depth += 1
            parent = by_id[parent][4]
        elements.append(
            Element(
                id=element_id,
                depth=depth,
                role=role,
                name=name,
                box=(0, 10 * element_id, 100, 10 * element_id + 10),
                runtime_id=runtime_id,
                parent_id=parent_id,
            )
        )
    return Snapshot(
        id=snapshot_id,
        hwnd=hwnd,
        process="chrome.exe",
        title=title,
        taken_at=taken_at,
        elements=elements,
        node_count=len(elements),
        stable=stable,
        handles=[e.runtime_id for e in elements],
    )


WINDOW = (0, "Pane", "Amir Khan - Chat", (42, 1), None)
CHATS = (1, "Group", "Chats", (42, 10), 0)
CHAT_LIST = (2, "List", "Chat list", (42, 11), 1)
CHAT_AMIR = (3, "ListItem", "Amir · hi bro · 2h", (42, 12), 2)
CHAT_SARA = (4, "ListItem", "Sara Ahmed · invoice attached · 1d", (42, 13), 2)
HEADER = (5, "Group", "Header", (42, 20), 0)
TITLE = (6, "Text", "Amir Khan", (42, 21), 5)
DELETE = (7, "Button", "Delete chat", (42, 22), 5)
CONVERSATION = (8, "Group", "Conversation", (42, 30), 0)
BUBBLE_OK = (9, "Text", "ok", (42, 31), 8)
BUBBLE_OUT = (10, "Text", "are we still on for friday?", (42, 32), 8)
COMPOSER_AREA = (11, "Group", "Composer", (42, 40), 0)
COMPOSER = (12, "Edit", "Message", (42, 41), 11)
SEND = (13, "Button", "Send", (42, 42), 11)
TOAST_AREA = (14, "Group", "Notifications", (42, 50), 0)

BASE_ROWS = [
    WINDOW,
    CHATS,
    CHAT_LIST,
    CHAT_AMIR,
    CHAT_SARA,
    HEADER,
    TITLE,
    DELETE,
    CONVERSATION,
    BUBBLE_OK,
    BUBBLE_OUT,
    COMPOSER_AREA,
    COMPOSER,
    SEND,
    TOAST_AREA,
]


def before() -> Snapshot:
    return build(BASE_ROWS, snapshot_id="before")


def after_with(*extra: Row) -> Snapshot:
    return build([*BASE_ROWS, *extra], snapshot_id="after", taken_at=1.0)


def after_without(*ids: int) -> Snapshot:
    return build([r for r in BASE_ROWS if r[0] not in ids], snapshot_id="after", taken_at=1.0)
