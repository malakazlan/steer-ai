"""A rich text Document always ends with a final paragraph mark (measured 2026-11-08: a fresh,
empty WordPad document reads `'\r'` through ValuePattern; issue #147). One final mark is never
content: the empty-field rule, the value check and the composer read all ignore it, for the
Document role only."""

from __future__ import annotations

from typing import Any

from steerai.action.dispositions import Delivery
from steerai.action.sanitize import field_text
from tests.action.test_physical import FakeInput, _edit, _executor, _snapshot, _type, fast_clock

__all__ = ["fast_clock"]  # the fixture, found by name


def test_one_final_mark_of_a_document_is_not_content() -> None:
    cr = chr(13)
    assert field_text(cr, "Document") == ""
    assert field_text("quarterly plan draft" + cr, "Document") == "quarterly plan draft"
    assert field_text("two" + cr + cr, "Document") == "two" + chr(10), "only one mark"
    assert field_text(cr, "Edit") == chr(10), "an Edit keeps it: not a rich text story"
    assert field_text("hello", "Document") == "hello"
    lf = chr(10)
    assert field_text(lf, "Document") == lf, "a final LF is content (a Chrome editor's newline)"
    assert field_text("x" + lf, "Document") == "x" + lf
    assert field_text("x" + cr + lf, "Document") == "x" + lf, "CRLF is a line, not the EOP"


def test_typing_into_an_empty_rich_text_document_is_not_refused(fast_clock: Any) -> None:
    handle = _edit(chr(13))
    inputs = FakeInput(sink=handle)
    result = _executor(inputs).execute(_type("hello"), _snapshot(handle, role="Document"))
    assert result.evidence.get("detail") != "field_not_empty", result.evidence
    assert inputs.bursts, "the text was typed"
    assert result.delivery is not Delivery.NOT_DELIVERED


def test_the_value_check_ignores_the_final_mark_of_a_document() -> None:
    from types import SimpleNamespace  # noqa: PLC0415 - this test only

    from steerai.verify.t1 import _read_value  # noqa: PLC0415
    from steerai.world.elements import Element  # noqa: PLC0415

    doc = Element(id=1, depth=0, role="Document", name="Rich Text Window", box=(0, 0, 9, 9))
    snapshot = SimpleNamespace(handle=lambda _i: "h")
    live = SimpleNamespace(value=lambda _h: "quarterly plan draft" + chr(13))
    assert _read_value(doc, snapshot, live) == "quarterly plan draft"  # type: ignore[arg-type]  # duck-typed fakes
