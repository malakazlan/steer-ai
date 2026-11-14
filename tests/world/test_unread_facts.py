"""[V2.26] (2), issue #301: the secure flag and the automation id are recorded as read or
unread, in sets beside the elements, so v1's view of every element is unchanged."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.probes.element_list import elements_from_nodes, unread_ids
from steerai.probes.uia_snapshot import RawNode, property_read, read_cached
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


@pytest.mark.parametrize(
    ("value", "secure", "read"),
    [(True, True, True), (False, False, True), (SENTINEL, False, False), (None, False, False)],
    ids=["password", "not_password", "not_supported", "none"],
)
def test_the_secure_flag_is_derived_from_one_typed_read(
    value: object, secure: bool, read: bool
) -> None:
    """One `GetCachedPropertyValueEx` per property, in place of `CachedIsPassword`: a bool is a
    read; the sentinel, None or any other type is unread and keeps v1's default False."""
    seen, raised = read_cached(_Cached(value), 30019, bool, SENTINEL)
    assert (bool(seen) if seen is not None else False, seen is not None) == (secure, read)
    assert raised is False


@pytest.mark.parametrize(
    ("value", "automation_id", "read"),
    [
        ("btnGo", "btnGo", True),
        ("", "", True),
        (SENTINEL, "", False),
        (None, "", False),
        (5, "", False),
    ],
    ids=["named", "empty", "not_supported", "none", "wrong_type"],
)
def test_the_automation_id_is_derived_from_one_typed_read(
    value: object, automation_id: str, read: bool
) -> None:
    seen, _ = read_cached(_Cached(value), 30011, str, SENTINEL)
    assert (seen if seen is not None else "", seen is not None) == (automation_id, read)


def test_a_wrong_type_is_never_a_read() -> None:
    assert read_cached(_Cached("yes"), 30019, bool, SENTINEL)[0] is None
    assert property_read(_Cached("yes"), 30019, SENTINEL, bool) is False


def test_a_provider_error_is_unread_and_counted() -> None:
    assert read_cached(_Cached(raises=True), 30019, bool, SENTINEL) == (None, True)


def test_a_supported_typed_property_is_read() -> None:
    assert property_read(_Cached(1), 30086, SENTINEL, int) is True
    assert property_read(_Cached(SENTINEL), 30086, SENTINEL, int) is False
