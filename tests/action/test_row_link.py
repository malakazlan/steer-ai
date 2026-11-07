"""A click on a row with no Invoke acts through its one link of the same name (a chat list's
`<li><a>` in Chrome; live #124 probe 2026-11-08: 4/4 clicks on the row refused `no_InvokePattern`).
Anything else stays refused: two links, a button inside, another name, an automation id."""

from __future__ import annotations

from typing import Any

from steerai.action.dispositions import Delivery
from steerai.action.execution import UiaBindings
from steerai.action.executor import Action, Executor
from steerai.errors import ErrorCode
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from tests.action.fakes import BOX, TYPE_IDS, UIA_INVOKE, FakeHandle, FakeWindows

ROW_NAME = "Amir Khan \u00b7 ok \u00b7 3d"


def _executor() -> Executor:
    return Executor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: 0.0,
    )


def _handle(role: str, name: str, *, invokable: bool) -> FakeHandle:
    handle = FakeHandle(patterns={UIA_INVOKE} if invokable else set())
    handle.control_type = TYPE_IDS[role]
    handle.name = name
    return handle


def _snapshot(children: list[tuple[str, str, str]]) -> tuple[Snapshot, list[FakeHandle]]:
    """A row (id 1) with child elements (role, name, automation id) as ids 2, 3, ..."""
    row = Element(id=1, depth=0, role="ListItem", name=ROW_NAME, box=BOX, runtime_id=(7, 1))
    elements = [row]
    handles = [_handle("ListItem", ROW_NAME, invokable=False)]
    for i, (role, name, automation_id) in enumerate(children, start=2):
        elements.append(
            Element(
                id=i,
                depth=1,
                role=role,
                name=name,
                box=BOX,
                runtime_id=(7, i),
                parent_id=1,
                automation_id=automation_id,
            )
        )
        handles.append(_handle(role, name, invokable=role in ("Hyperlink", "Button")))
    snapshot = Snapshot(
        id="s1",
        hwnd=0x10,
        process="chrome.exe",
        title="t",
        taken_at=0.0,
        elements=elements,
        node_count=len(elements),
        stable=True,
        handles=list(handles),
    )
    return snapshot, handles


def _click(snapshot: Snapshot) -> Any:
    return _executor().execute(Action(verb="click", element_id=1), snapshot)


def test_a_row_without_invoke_acts_through_its_one_link_of_the_same_name() -> None:
    snapshot, handles = _snapshot([("Hyperlink", ROW_NAME, "")])
    result = _click(snapshot)
    assert handles[1].invoked == 1
    assert handles[0].invoked == 0
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED  # no postcondition given
    assert result.evidence["acted_on"] == "row_link"
    assert result.steps[0].label == "InvokePattern.Invoke"


def test_two_links_stay_refused() -> None:
    snapshot, handles = _snapshot([("Hyperlink", ROW_NAME, ""), ("Hyperlink", ROW_NAME, "")])
    result = _click(snapshot)
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.delivery is Delivery.NOT_DELIVERED
    assert all(h.invoked == 0 for h in handles)


def test_a_button_inside_the_row_keeps_it_refused() -> None:
    """A row holding "Delete chat" must never act through anything but the row."""
    snapshot, handles = _snapshot([("Hyperlink", ROW_NAME, ""), ("Button", "Delete chat", "")])
    result = _click(snapshot)
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert all(h.invoked == 0 for h in handles)


def test_a_link_with_another_name_stays_refused() -> None:
    snapshot, handles = _snapshot([("Hyperlink", "Open profile", "")])
    result = _click(snapshot)
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert handles[1].invoked == 0


def test_a_link_with_an_automation_id_stays_refused() -> None:
    """The gate read the row's id, not the link's (#84): an id it never judged never acts."""
    snapshot, handles = _snapshot([("Hyperlink", ROW_NAME, "deleteThread")])
    result = _click(snapshot)
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert handles[1].invoked == 0


def test_a_row_with_invoke_is_clicked_itself() -> None:
    snapshot, handles = _snapshot([("Hyperlink", ROW_NAME, "")])
    handles[0].patterns = {UIA_INVOKE}
    _click(snapshot)
    assert handles[0].invoked == 1
    assert handles[1].invoked == 0
