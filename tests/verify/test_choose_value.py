"""The folding comparator is for a choose's ComboBox value only (W4-05, as corrected by the
principal on W4 v3): `normalise_name` (NFKC folds a no-break space, whitespace folded, case
kept) for the choose template and a `value` its choose proves; a `type` and its `value` keep the
exact rule, so "a b" never passes for a field reading "a" LF "b" in Notepad."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from steerai.verify.predicates import FieldValue, Predicate, ValueEquals
from steerai.verify.t1 import evaluate
from tests.verify.fakes import BASE_ROWS, COMPOSER, FakeHandle, FakeLive, Row, build

NBSP = chr(0xA0)
LF = chr(10)
COMBO: Row = (COMPOSER[0], "ComboBox", "Message", COMPOSER[3], COMPOSER[4])
ROWS = [COMBO if r == COMPOSER else r for r in BASE_ROWS]


def _live(value: str) -> FakeLive:
    return FakeLive(states={COMPOSER[3]: FakeHandle(value=value)})


def _evaluate(predicate: Predicate, value: str, rows: Sequence[Row] = ROWS) -> bool:
    before = build(list(rows), snapshot_id="before")
    after = build(list(rows), snapshot_id="after", taken_at=1.0)
    return evaluate(predicate, before, after, live=_live(value)).holds


def test_a_choose_value_with_a_no_break_space_holds() -> None:
    chosen = FieldValue(role="ComboBox", name_pattern="Message", text="New York", folded=True)
    assert _evaluate(chosen, f"New{NBSP}York") is True


def test_the_choose_template_folds_a_no_break_space() -> None:
    template = ValueEquals(target_id=COMPOSER[0], text="New York", folded=True)
    assert _evaluate(template, f"New{NBSP}York") is True


def test_the_folding_comparator_keeps_case() -> None:
    chosen = FieldValue(role="ComboBox", name_pattern="Message", text="Large", folded=True)
    assert _evaluate(chosen, "large") is False


def test_a_typed_value_reading_a_newline_never_passes_for_a_space() -> None:
    typed = FieldValue(role="Edit", name_pattern="Message", text="a b")
    assert _evaluate(typed, f"a{LF}b", BASE_ROWS) is False
    assert _evaluate(replace(typed, text=f"a{LF}b"), f"a{LF}b", BASE_ROWS) is True


def test_the_type_template_keeps_the_exact_rule() -> None:
    typed = ValueEquals(target_id=COMPOSER[0], text="a b")
    assert _evaluate(typed, f"a{LF}b", BASE_ROWS) is False


def test_a_folded_value_never_aliases_to_another_field_role() -> None:
    # a choose names the ComboBox itself: an Edit of the name never proves it (W4-10)
    chosen = FieldValue(role="ComboBox", name_pattern="Message", text="hello", folded=True)
    assert _evaluate(chosen, "hello", BASE_ROWS) is False


def test_a_typed_value_never_aliases_to_a_combobox() -> None:
    # W4-10, the condition side of the step's alias rule: a ComboBox's value is a choose's
    typed = FieldValue(role="Edit", name_pattern="Message", text="hello")
    assert _evaluate(typed, "hello") is False
    assert _evaluate(typed, "hello", BASE_ROWS) is True
