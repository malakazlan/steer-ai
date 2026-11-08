"""A `value` condition on twin fields (#150, D-150 v2 item 4; hunter TW-14): it reads only the
twin the verified `type` step wrote (the pin, keyed by window, role and name), never the one
on screen; with no pin, or a pin on neither twin, it stays ambiguous."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from steerai.verify.predicates import FieldValue
from steerai.verify.t1 import evaluate
from tests.verify.fakes import FakeHandle, FakeLive
from tests.world.test_twins import BOTTOM, TOP, _page, _typed

CONDITION = FieldValue(role="Edit", name_pattern="Quick search", text="pathlib")
KEY = (0x10, "Edit", "quick search")
QUERY_TEXT = "pathlib"


def _judge(pinned: dict[tuple[int, str, str], tuple[int, ...]], top: str, bottom: str) -> str:
    snap = _page(TOP, BOTTOM)
    snap = replace(snap, handles=[e.runtime_id for e in snap.elements])
    live = FakeLive(
        states={TOP.runtime_id: FakeHandle(value=top), BOTTOM.runtime_id: FakeHandle(value=bottom)}
    )
    verdict = evaluate(CONDITION, snap, snap, live=live, pinned=MappingProxyType(pinned))
    return "holds" if verdict.holds else str(verdict.evidence.get("reason"))


def test_the_pinned_twin_is_read() -> None:
    assert _judge({KEY: TOP.runtime_id}, "pathlib", "") == "holds"
    assert _judge({KEY: BOTTOM.runtime_id}, "pathlib", "") == "value_empty", "never the other"


def test_no_pin_or_a_pin_on_neither_twin_is_ambiguous() -> None:
    assert _judge({}, "pathlib", "") == "ambiguous_target"
    assert _judge({KEY: (999,)}, "pathlib", "") == "ambiguous_target"


def test_a_pin_from_another_window_is_ignored() -> None:
    assert _judge({(0x20, "Edit", "quick search"): TOP.runtime_id}, "pathlib", "") == (
        "ambiguous_target"
    )


def test_a_raw_none_on_the_plain_twin_is_unreadable_not_empty() -> None:
    """Review of #174, blocking 1: one meaning of unreadable for the loop and the condition."""
    snap = _typed(QUERY_TEXT, top_row=QUERY_TEXT + "Go|")
    snap = replace(snap, handles=[e.runtime_id for e in snap.elements])
    live = FakeLive(
        states={TOP.runtime_id: FakeHandle(value=QUERY_TEXT), BOTTOM.runtime_id: FakeHandle()}
    )
    pinned = MappingProxyType({KEY: TOP.runtime_id})
    verdict = evaluate(CONDITION, snap, snap, live=live, pinned=pinned)
    assert verdict.evidence.get("reason") == "ambiguous_target"
    live.states[BOTTOM.runtime_id] = FakeHandle(value="")
    assert evaluate(CONDITION, snap, snap, live=live, pinned=pinned).holds is True
