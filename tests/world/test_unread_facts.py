"""[V2.26] (2), issue #301: the secure flag and the automation id are recorded as read or
unread, in sets beside the elements, so v1's view of every element is unchanged."""

from __future__ import annotations

import dataclasses

from steerai.probes.element_list import elements_from_nodes, unread_ids
from steerai.probes.uia_snapshot import RawNode, property_read
from steerai.world.elements import Element
from steerai.world.model import _ELEMENT_FIELDS, Snapshot, snapshot_to_json

BOX = (0, 0, 10, 10)
READ = RawNode(depth=0, role="Edit", name="Code", automation_id="", box=BOX, offscreen=False)
SENTINEL = object()


def test_unread_flags_change_no_element() -> None:
    unread = dataclasses.replace(READ, secure_read=False, automation_id_read=False)
    assert elements_from_nodes([unread]) == elements_from_nodes([READ])


def test_unread_ids_are_the_element_ids_of_the_unread_nodes() -> None:
    nodes = [READ, dataclasses.replace(READ, secure_read=False), READ]
    assert unread_ids(nodes, "secure") == frozenset({2})
    assert unread_ids(nodes, "automation_id") == frozenset()


def test_v1s_element_fields_are_unchanged() -> None:
    """A fixed-value check: the write-ahead record's element keys are exactly v1's."""
    assert _ELEMENT_FIELDS == (
        "id",
        "depth",
        "role",
        "name",
        "box",
        "offscreen",
        "enabled",
        "automation_id",
        "runtime_id",
        "parent_id",
        "secure",
        "caps",
    )
    assert set(_ELEMENT_FIELDS) == {f.name for f in dataclasses.fields(Element)}


def test_the_write_ahead_json_is_identical_with_or_without_unread_sets() -> None:
    element = elements_from_nodes([READ])
    plain = Snapshot(
        id="s",
        hwnd=1,
        process="p",
        title="t",
        taken_at=0.0,
        elements=element,
        node_count=1,
        stable=True,
    )
    marked = dataclasses.replace(
        plain, unread_secure=frozenset({1}), unread_automation_id=frozenset({1})
    )
    assert snapshot_to_json(marked) == snapshot_to_json(plain)


class _Cached:
    def __init__(self, value: object = None, *, raises: bool = False) -> None:
        self.value, self.raises = value, raises

    def GetCachedPropertyValueEx(self, prop: int, ignore_default: bool) -> object:  # noqa: N802, ARG002 - the COM name and shape
        if self.raises:
            raise OSError("provider failed")
        assert ignore_default is True, "the not-supported sentinel needs ignoreDefaultValue"
        return self.value


def test_a_supported_property_is_read() -> None:
    assert property_read(_Cached(True), 30019, SENTINEL) is True
    assert property_read(_Cached(False), 30019, SENTINEL) is True, "False is a read value"


def test_the_not_supported_sentinel_is_unread() -> None:
    assert property_read(_Cached(SENTINEL), 30019, SENTINEL) is False


def test_a_provider_error_is_unread() -> None:
    assert property_read(_Cached(raises=True), 30019, SENTINEL) is False
