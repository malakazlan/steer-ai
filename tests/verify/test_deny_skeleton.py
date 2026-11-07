"""The closure form of the deny matcher (5.13 1a [v2.26], #83): cell precedence, every lookalike
of every pattern letter, combinations of two features, the plain matcher's hits kept, and the
known gaps pinned as strict xfails under their issues."""

from __future__ import annotations

import random
import string

import pytest

from steerai.orchestrator.gate import CONTROL_PATTERNS, DENY_PATTERNS, _contains, _words
from steerai.verify.confusables import CONFUSABLE_TO_LATIN
from steerai.verify.deny_skeleton import camel_split, cells, holds

PATTERNS = sorted({*DENY_PATTERNS, *CONTROL_PATTERNS})
SOFT_I = chr(0x00A1)  # inverted exclamation: not a word character, its prototype is i
CGJ = chr(0x034F)  # combining grapheme joiner: invisible
CYR_IE = chr(0x0435)


def _sources(letter: str) -> list[str]:
    """Every table source whose prototype is the letter, with its capital when that is one
    non-ASCII character whose casefold is the source (long s has the capital S: not a twin)."""
    found: list[str] = []
    for source, target in CONFUSABLE_TO_LATIN.items():
        if target != letter:
            continue
        found.append(source)
        upper = source.upper()
        if len(upper) == 1 and not upper.isascii() and upper.casefold() == source:
            found.append(upper)
    return found


# ---- cells: the precedence ----------------------------------------------------------------


def test_cell_precedence_blank_table_ascii_letter_foreign_hard() -> None:
    assert cells(chr(0x3164)) == ("~", "~"), "a Hangul filler is blank before and after NFKC"
    assert cells(chr(0x0C02)) == ("O", "O"), "a spacing mark in the table is a soft letter"
    assert cells("I") == ("L", "i"), "I is a soft l before casefold, an i after"
    assert cells("0|1") == ("OLL", "OLL")
    assert cells(f"S{CYR_IE}nd") == ("send", "send")
    assert cells(chr(0x00E9)) == ("#", "#"), "a letter with no prototype is foreign"
    assert cells("a-b_c 2") == ("a b c  ", "a b c  ")
    assert cells("Don" + chr(0x02BC) + "t") == ("don t", "don t"), "a modifier apostrophe"


def test_a_capital_and_its_small_letter_fold_in_their_own_orders() -> None:
    nu = chr(0x039D)  # Greek capital Nu: n before casefold, its small letter v after
    assert cells(nu) == ("n", "v")


def test_a_pattern_must_be_lowercase_ascii_words() -> None:
    with pytest.raises(ValueError, match="lowercase ASCII"):
        holds("x", "Send")
    for pattern in PATTERNS:
        assert pattern == pattern.lower()
        assert pattern.isascii()


def test_camel_split_follows_unicode_case() -> None:
    assert camel_split("btnDelete") == "btn Delete"
    assert camel_split("BTNDelete") == "BTN Delete"
    assert camel_split("btn" + chr(0x0405) + "end") == "btn " + chr(0x0405) + "end"
    assert camel_split("item2Delete") == "item2 Delete"


# ---- coverage: one feature, every lookalike ----------------------------------------------


def test_every_lookalike_of_every_pattern_letter_is_read() -> None:
    cases = 0
    for pattern in PATTERNS:
        for position, letter in enumerate(pattern):
            if letter == " ":
                continue
            for source in _sources(letter):
                name = pattern[:position] + source + pattern[position + 1 :]
                for text in (name, SOFT_I + name, name + SOFT_I):
                    assert holds(text, pattern), (pattern, hex(ord(source)), ascii(text))
                    cases += 1
    assert cases > 10_000, cases


# ---- coverage: two features at once -------------------------------------------------------

_SECOND = {
    "ascii": lambda word: word.replace("l", "1", 1).replace("o", "0", 1).replace("i", "1", 1),
    "soft": lambda word: word + SOFT_I,
    "blank": lambda word: word[:1] + CGJ + word[1:],
}


@pytest.mark.parametrize("second", sorted(_SECOND))
def test_a_lookalike_letter_with_a_second_feature_is_read(second: str) -> None:
    cases = 0
    for pattern in PATTERNS:
        first_letter = next(c for c in pattern if c != " ")
        for source in _sources(first_letter)[:8]:
            name = _SECOND[second](source + pattern[1:])
            upper = "".join(c.upper() if c.isascii() else c for c in name)  # the lookalike kept
            for text in (name, upper, upper[:1] + name[1:]):
                assert holds(text, pattern), (pattern, second, ascii(text))
                cases += 1
    assert cases > 500, cases


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("D" + CYR_IE + "1ete", "delete"),
        (chr(0x0405) + "ubm1t", "submit"),
        ("U" + chr(0x039D) + "INSTA" + chr(0x0661) + "L", "uninstall"),
        ("Send" + chr(0x3164) + "Now", "send"),
    ],
)
def test_the_re_review_combinations_are_read(text: str, pattern: str) -> None:
    assert holds(text, pattern)


# ---- the plain matcher's hits are the closure's too ---------------------------------------

_ALPHABET = string.ascii_letters + " ,.()_-'2" + chr(0x02BC) + chr(0x200B) + chr(0x00A0)


def _decorated(pattern: str, rng: random.Random) -> str:
    noise = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 6)))
    word = "".join(c.upper() if rng.random() < 0.3 else c for c in pattern)
    word = word.replace(" ", rng.choice([" ", "  ", "'", chr(0x02BC), "-", " - "]))
    return noise + rng.choice(["", " ", ",", "("]) + word + rng.choice(["", " ", ")", "2"]) + noise


def test_the_closure_alone_reads_every_name_the_plain_matcher_reads() -> None:
    rng = random.Random(83)  # noqa: S311 - a seeded corpus for a deterministic test
    texts = [_decorated(p, rng) for p in PATTERNS for _ in range(60)]
    texts += [
        "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 24))) for _ in range(4000)
    ]
    plain_hits = 0
    for text in texts:
        words = _words(text)
        for pattern in PATTERNS:
            if _contains(words, pattern):
                plain_hits += 1
                assert holds(text, pattern), (pattern, ascii(text))
    assert plain_hits > 1000, plain_hits


# ---- known gaps (strict xfails under their issues) ----------------------------------------


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        pytest.param(
            "Delete" + chr(0x01C3),
            "delete",
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason="HG-09 #100"),
        ),
        pytest.param(
            "".join(chr(c) for c in (0x1D05, 0x1D07, 0x029F, 0x1D07, 0x1D1B, 0x1D07)),
            "delete",
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason="HG-10 #99"),
        ),
        pytest.param(
            "".join(chr(c) for c in (0x1F182, 0x1F174, 0x1F17D, 0x1F173)),
            "send",
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason="HG-11 #99"),
        ),
        pytest.param(
            "Confirrn",
            "confirm",
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason="HG-12 #99"),
        ),
        pytest.param(
            chr(0x202E) + "dnes",
            "send",
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason="HG-14 #101"),
        ),
        pytest.param(
            chr(0x200A).join("Send"),
            "send",
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason="HG-15 #101"),
        ),
        pytest.param(
            "Dont Save",
            "don t save",
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason="HG-22 #102"),
        ),
    ],
)
def test_a_known_gap_is_read_once_fixed(text: str, pattern: str) -> None:
    assert holds(text, pattern) or _contains(_words(text), pattern)
