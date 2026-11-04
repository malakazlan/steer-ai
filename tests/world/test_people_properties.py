"""Property-style checks on the person rule: the comparable form is idempotent (with the stated
Greek exceptions), every plain name matches itself, nothing matches an empty parameter, the
vendored table is the generator's output and its keys fold to one letter (with the stated
NFKC exceptions)."""

from __future__ import annotations

import random
import unicodedata
from pathlib import Path

import pytest

from steerai.world.people import (
    CONFUSABLE_TO_LATIN,
    comparable,
    generate_confusable_table,
    matches,
    name_part,
)

DATA = Path(__file__).resolve().parents[1] / "data" / "confusables.txt"
MID = "\u00b7"
LETTERS = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0410\u0412\u0415\u041a\u041c\u041d\u041e"
    "\u03b1\u03bf\u03bd\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1"
    "\u00e9\u00fc\u00f1\u00e5\u0131\u00df"
    "0123456789"
)
JOINERS = (" ", " ", " ", "_", "(", ")", "'", ".", "\u00a0", "\u200b")
SAMPLE = 400

NON_IDEMPOTENT = {"\u0390", "\u03b0", "\u1fd2", "\u1fd3", "\u1fd7", "\u1fe2", "\u1fe3", "\u1fe7"}
"""Greek upsilon with dialytika and an accent: casefold decomposes it, the confusable fold then
maps the bare upsilon to a Latin letter and NFKC recomposes it with the marks on a second pass.
A stated limit of comparable (5.8 [v1.5]), not a matching bug: both sides take one pass."""

NFKC_SHADOWED = 16
"""Table keys NFKC maps to ASCII (fullwidth and mathematical digits, a vertical em dash):
`comparable` never reaches them. Dead entries, stated here so a change in the count is seen."""

CASEFOLD_SHADOWED = 43  # own lines win over casefold-derived keys (review item 1)
"""Uppercase confusables whose lowercase form has another prototype (Greek capital Nu looks like
N, nu looks like v): case-folding runs first, so the uppercase reading is never applied."""


def _names(seed: int = 7) -> list[str]:
    rng = random.Random(seed)  # noqa: S311 - a seeded corpus for a deterministic test
    out: list[str] = []
    for _ in range(SAMPLE):
        tokens = [
            "".join(rng.choice(LETTERS) for _ in range(rng.randint(1, 8)))
            for _ in range(rng.randint(1, 3))
        ]
        name = tokens[0]
        for token in tokens[1:]:
            name += rng.choice(JOINERS) + token
        out.append(name)
    return out


def _plain(name: str) -> bool:
    return bool(comparable(name).strip()) and name_part(name) == name.strip()


# ---- comparable ----------------------------------------------------------------------------------


def test_comparable_is_idempotent_on_every_code_point_but_the_stated_greek_exceptions() -> None:
    failing = {
        chr(i) for i in range(0x20, 0x30000) if comparable(comparable(chr(i))) != comparable(chr(i))
    }
    assert failing == NON_IDEMPOTENT, sorted(hex(ord(c)) for c in failing)


def test_comparable_is_idempotent_on_generated_names_and_on_the_table() -> None:
    for name in [*_names(), *CONFUSABLE_TO_LATIN, *CONFUSABLE_TO_LATIN.values()]:
        once = comparable(name)
        assert comparable(once) == once, name.encode("unicode_escape")


def test_comparable_is_lowercase_without_format_characters_or_surviving_confusables() -> None:
    for name in _names():
        folded = comparable(name)
        assert folded == folded.casefold()
        assert all(unicodedata.category(ch) != "Cf" for ch in folded)
        assert all(ch not in CONFUSABLE_TO_LATIN for ch in folded), "no confusable survives"


def test_comparable_of_a_table_key_is_one_ascii_letter_unless_nfkc_shadows_it() -> None:
    shadowed = {
        k: comparable(k)
        for k in CONFUSABLE_TO_LATIN
        if not (len(comparable(k)) == 1 and "a" <= comparable(k) <= "z")
    }
    assert len(shadowed) == NFKC_SHADOWED, {hex(ord(k)): v for k, v in shadowed.items()}
    assert all(not v.isalpha() for v in shadowed.values()), "no shadowed key is a letter"


def test_uppercase_confusables_fold_through_their_lowercase_form_a_stated_limit() -> None:
    # Greek capital Nu looks like N (the table says n) but case-folds to nu, which looks like v.
    assert CONFUSABLE_TO_LATIN["\u039d"] == "n"
    assert comparable("\u039d") == "v"
    assert matches("Nikos", "\u039dikos") is False, "an uppercase-only lookalike is not found"
    differing = sorted(
        hex(ord(k))
        for k, v in CONFUSABLE_TO_LATIN.items()
        if comparable(k) != v and comparable(k).isalpha()
    )
    assert len(differing) == CASEFOLD_SHADOWED, differing


# ---- matches -------------------------------------------------------------------------------------


def test_every_plain_name_matches_itself_its_own_row_and_its_whole_token_extension() -> None:
    names = [n for n in _names() if _plain(n)]
    assert len(names) > SAMPLE // 2, "the corpus is mostly plain names"
    for name in names:
        assert matches(name, name) is True, name.encode("unicode_escape")
        assert matches(name, f"{name} {MID} preview {MID} 2h") is True
        assert matches(name, f"{name} Khan") is True
        assert matches(name, f"{name}_work") is True
        assert matches(name, f"{name}(2)") is True
        assert matches(name, f"{name}-Jane") is False
        assert matches(name, f"{name}a") is False
        assert matches(name, f"Dr {name}") is False


def test_matching_is_invariant_under_case_and_width_of_either_side() -> None:
    for name in [n for n in _names(seed=11) if _plain(n)]:
        row = f"{name} {MID} hi"
        assert matches(name.upper(), row) is True
        assert matches(name.lower(), row) is True
        assert matches(name, row.upper()) is True
        wide = "".join(chr(ord(c) + 0xFEE0) if "a" <= c <= "z" else c for c in name)
        assert matches(wide, row) is True, name


def test_matches_implies_the_comparable_prefix_relation() -> None:
    names = _names(seed=3)
    for parameter in names[:60]:
        for row in names:
            if matches(parameter, row):
                assert comparable(name_part(row)).startswith(comparable(parameter).strip())


@pytest.mark.parametrize(
    "parameter",
    ["", " ", "\t", "\u200b", "\ufeff\u200d", "  \u200b  ", "\u2060"],
)
def test_an_empty_or_invisible_parameter_matches_nothing(parameter: str) -> None:
    for row in ("Amir", "", " ", "\u200b", f"{MID} hi", f"Amir {MID} hi", *_names()[:50]):
        assert matches(parameter, row) is False


def test_a_row_with_an_empty_name_part_matches_no_parameter() -> None:
    for row in ("", " ", MID, f"{MID} Amir", " - Amir", "\u200b", f"\u200b {MID} Amir"):
        for parameter in ("Amir", "\u200b", MID, "-", *_names()[:20]):
            assert matches(parameter, row) is False, (parameter, row)


# ---- the table -----------------------------------------------------------------------------------


def test_the_generator_is_deterministic_sorted_and_equal_to_the_vendored_table() -> None:
    text = DATA.read_text(encoding="utf-8-sig")
    first = generate_confusable_table(text)
    second = generate_confusable_table(text)
    assert first == second
    assert list(first) == sorted(first)
    assert first == CONFUSABLE_TO_LATIN
    assert list(CONFUSABLE_TO_LATIN) == sorted(CONFUSABLE_TO_LATIN)


def test_the_generator_ignores_comments_blank_lines_multi_code_point_and_non_letters() -> None:
    sample = "\n".join(
        [
            "\ufeff# Version: 18.0.0",
            "",
            "0430 ;\t0061 ;\tMA\t# ( a -> a ) CYRILLIC SMALL LETTER A -> LATIN SMALL LETTER A",
            "0410 ;\t0041 ;\tMA\t# capital",
            "0027 ;\t0060 ;\tMA\t# ( ' -> ` ) not a letter",
            "0049 ;\t006C ;\tMA\t# ( I -> l ) ASCII source is never a key",
            "FB00 ;\t0066 0066 ;\tMA\t# ( ff -> ff ) multi code point target",
            "0063 0068 ;\t0068 ;\tMA\t# multi code point source",
            "garbage line without fields",
        ]
    )
    table = generate_confusable_table(sample)
    assert table == {"\u0410": "a", "\u0430": "a"}


def test_the_generator_on_an_empty_file_is_an_empty_table() -> None:
    assert generate_confusable_table("") == {}
    assert generate_confusable_table("# only comments\n\n") == {}
