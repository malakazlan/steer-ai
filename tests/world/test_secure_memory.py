"""Secure is sticky by RuntimeId for the task (WV-09, W2 verdict item 3)."""

from __future__ import annotations

from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.secure import SecureMemory

PASSWORD = (42, 7, 4, 6, 1, 2)


def _snapshot(*elements: Element) -> Snapshot:
    return Snapshot(
        id="s",
        hwnd=1,
        process="chrome.exe",
        title="Sign in",
        taken_at=0.0,
        elements=list(elements),
        node_count=len(elements),
        stable=True,
    )


def _field(*, secure: bool, runtime_id: tuple[int, ...] = PASSWORD, eid: int = 1) -> Element:
    return Element(
        id=eid,
        depth=1,
        role="Edit",
        name="Password",
        box=(0, 0, 100, 20),
        runtime_id=runtime_id,
        secure=secure,
    )


def test_a_field_seen_secure_stays_secure_after_show_password() -> None:
    memory = SecureMemory()
    memory.apply(_snapshot(_field(secure=True)))
    shown = memory.apply(_snapshot(_field(secure=False)))
    assert shown.elements[0].secure is True
    assert " secure" in shown.lines[0]


def test_other_fields_and_fields_never_seen_secure_are_untouched() -> None:
    memory = SecureMemory()
    memory.apply(_snapshot(_field(secure=True)))
    later = _snapshot(
        _field(secure=False, runtime_id=(42, 7, 4, 6, 1, 3), eid=2),
        _field(secure=False, runtime_id=(), eid=3),
    )
    assert memory.apply(later) is later
    assert SecureMemory().apply(_snapshot(_field(secure=False))).elements[0].secure is False


def test_an_element_without_a_runtime_id_is_never_remembered() -> None:
    memory = SecureMemory()
    memory.apply(_snapshot(_field(secure=True, runtime_id=())))
    assert memory.apply(_snapshot(_field(secure=False, runtime_id=()))).elements[0].secure is False
