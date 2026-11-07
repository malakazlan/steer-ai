"""The person-matching rule (design 5.8 [v0.9]) and its confusable table from UTS #39."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerai.verify.confusables import (
    CONFUSABLE_TO_LATIN,
    UNICODE_VERSION,
    generate_confusable_table,
)
from steerai.world.elements import Element
from steerai.world.people import candidates, comparable, matches, name_part

DATA = Path(__file__).resolve().parents[1] / "data" / "confusables.txt"


# ---- the table is generated, not remembered ---------------------------------------------------


def test_the_vendored_table_equals_the_generator_output_from_the_unicode_data() -> None:
    text = DATA.read_text(encoding="utf-8-sig")
    assert f"Version: {UNICODE_VERSION}" in text
    assert generate_confusable_table(text) == CONFUSABLE_TO_LATIN


def test_the_table_maps_single_non_ascii_letters_to_single_lowercase_latin_letters() -> None:
    assert CONFUSABLE_TO_LATIN
    for source, target in CONFUSABLE_TO_LATIN.items():
        assert len(source) == 1
        assert not source.isascii()
        assert len(target) == 1
        assert "a" <= target <= "z"
    assert CONFUSABLE_TO_LATIN["\u0430"] == "a"  # Cyrillic a
    assert CONFUSABLE_TO_LATIN["\u03bf"] == "o"  # Greek omicron
    assert CONFUSABLE_TO_LATIN["\u0440"] == "p"  # Cyrillic er


# ---- the comparable form ------------------------------------------------------------------------


def test_comparable_folds_case_format_characters_and_confusables() -> None:
    assert comparable("Amir") == "amir"
    assert comparable("A\u200bmir") == "amir"
    assert comparable("\uff21mir") == "amir"  # fullwidth A through NFKC
    assert comparable("\u0410mir") == "amir"  # Cyrillic capital A
    assert comparable("\u0391mir") == "amir"  # Greek capital alpha


def test_name_part_is_the_text_before_the_first_separator() -> None:
    assert name_part("Amir Khan \u00b7 ok \u00b7 3d") == "Amir Khan"
    assert name_part("Amir Khan\u00b7ok") == "Amir Khan"
    assert name_part("Amir Khan - ok") == "Amir Khan"
    assert name_part("Amir Khan") == "Amir Khan"
    assert name_part("\u00b7 ok") == ""
    assert name_part("   ") == ""


# ---- the matching rule --------------------------------------------------------------


@pytest.mark.parametrize(
    ("parameter", "name", "expected"),
    [
        ("Amir", "Amir \u00b7 hi bro \u00b7 2h", True),
        ("Amir", "Amir Khan \u00b7 ok \u00b7 3d", True),
        ("amir khan", "Amir Khan \u00b7 ok", True),
        ("Amir", "Amira \u00b7 hey", False),
        ("Amir", "Amir-Khan \u00b7 hey", False),
        ("Amir", "Amir_Khan", True),
        ("Amir", "Amir (work) \u00b7 hey", True),
        ("Amir", "Dr Amir \u00b7 hey", False),
        ("Amir", "\u0410mir \u00b7 hey", True),  # Cyrillic twin matches: it must be asked about
        ("Amir", "", False),
        ("", "Amir", False),
        ("Amir Khan", "Amir \u00b7 hey", False),
    ],
)
def test_matches_is_equality_or_a_whole_token_prefix_of_the_name_part(
    parameter: str, name: str, expected: bool
) -> None:
    assert matches(parameter, name) is expected


def _row(index: int, name: str, role: str = "ListItem") -> Element:
    return Element(
        id=index,
        depth=1,
        role=role,
        name=name,
        box=(0, index * 10, 100, index * 10 + 10),
        runtime_id=(42, index),
        parent_id=0,
    )


def test_candidates_keep_document_order_and_the_role() -> None:
    rows = [
        _row(1, "Amir \u00b7 hi bro \u00b7 2h"),
        _row(2, "Sara \u00b7 ok"),
        _row(3, "Amir Khan \u00b7 ok \u00b7 3d"),
        _row(4, "Amir", role="Button"),
    ]
    assert [e.id for e in candidates(rows, "ListItem", "Amir")] == [1, 3]
    assert candidates(rows, "ListItem", "Sara Ahmed") == []
