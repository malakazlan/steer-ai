"""The deny matcher's closure form (5.13 1a [v2.26], #83): a name that reads as a deny word on
screen matches it, whatever code points spell it.

The text becomes a string of cells, once per case order (fold before casefold, and casefold
before fold: a capital and its small letter can have different prototypes, Greek Nu):

- `~` soft blank: anything invisible (Default_Ignorable_Code_Point, format characters, combining
  marks, control characters other than whitespace). It is nothing or a boundary.
- `a`-`z`: a letter, ASCII or a lookalike that is a word character, folded to its prototype.
- `A`-`Z` soft letter: a lookalike that is not a word character (U+00A1 for i, Arabic-Indic one
  for l) and the gate-only ASCII lookalikes (0, 1, |, and I before casefold). It is its letter
  or a boundary; a soft i or l stands for either (Subm1t, DeIete).
- `#`: any other word character. Part of a word; it never stands for a pattern letter.
- ` `: a boundary (whitespace, punctuation, other digits, `_`, modifier apostrophes).

Each pattern word is one regex with no ambiguous quantifier (`~*+` never overlaps a letter
class), so a search is linear in the text. Words of a multi-word pattern chain when the cells
between them are all boundaries, soft blanks or soft letters, at least one. Patterns are code
constants of lowercase ASCII words: no regex is ever built from screen text.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from functools import cache, lru_cache

from steerai.verify.confusables import ASCII_LOOKALIKES, CONFUSABLE_TO_LATIN, default_ignorable

BLANK = "~"
FOREIGN = "#"
HARD = " "
_GAP = frozenset(" ~ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_PATTERN = re.compile(r"[a-z]+(?: [a-z]+)*")
_APOSTROPHES = frozenset(chr(code) for code in (0x02B9, 0x02BB, 0x02BC, 0x02BD))
"""Modifier-letter apostrophes are word characters to the plain matcher's regex, but a
separator to it after its translation (hunter DC-15); the closure keeps them boundaries."""
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Mn", "Me"})


def _word(ch: str) -> bool:
    """A word character of the plain matcher's `[^\\W\\d_]`: alphanumeric, not a decimal digit."""
    return ch.isalnum() and not ch.isdecimal()


def _blank(ch: str) -> bool:
    category = unicodedata.category(ch)
    if category in _INVISIBLE_CATEGORIES or default_ignorable(ch):
        return True
    return category == "Cc" and not ch.isspace()


def _known(ch: str, *, capital_i: bool) -> str | None:
    """The cell of a character the tables or the ASCII letters decide, by precedence."""
    if _blank(ch):
        return BLANK
    prototype = CONFUSABLE_TO_LATIN.get(ch)
    if prototype is not None:
        return prototype if _word(ch) else prototype.upper()
    lookalike = ASCII_LOOKALIKES.get(ch)
    if lookalike is not None and (ch != "I" or capital_i):
        return lookalike.upper()
    if "a" <= ch <= "z":
        return ch
    return None


def _rest(ch: str) -> str:
    if ch in _APOSTROPHES or not _word(ch):
        return HARD
    return FOREIGN


def _cells_of(text: str, *, fold_first: bool) -> str:
    out: list[str] = []
    for raw in text:
        if _blank(raw):
            out.append(BLANK)
            continue
        if fold_first and raw in CONFUSABLE_TO_LATIN:
            # the raw character's own line first: NFKC can move it to another prototype
            # (lunate sigma reads c, its NFKC final sigma reads o)
            out.append(_known(raw, capital_i=True) or HARD)
            continue
        for ch in unicodedata.normalize("NFKC", raw):
            cell = _known(ch, capital_i=True) if fold_first else None
            if cell is not None:
                out.append(cell)
                continue
            for small in ch.casefold():
                known = _known(small, capital_i=False)
                out.append(_rest(small) if known is None else known)
    return "".join(out)


@lru_cache(maxsize=512)
def cells(text: str) -> tuple[str, str]:
    """The text's cells in both case orders: fold first, then casefold first."""
    return _cells_of(text, fold_first=True), _cells_of(text, fold_first=False)


@cache
def _compiled(pattern: str) -> tuple[re.Pattern[str], ...]:
    if not _PATTERN.fullmatch(pattern):
        raise ValueError("a deny pattern is lowercase ASCII words")
    return tuple(
        re.compile("(?<![a-z#])" + "~*+".join(_letter(c) for c in word) + "(?![a-z#])")
        for word in pattern.split()
    )


def _letter(c: str) -> str:
    """A pattern letter's class: the letter, its soft form, and for i and l both soft forms (a
    1, an I or a | reads as either)."""
    if c in "il":
        return f"[{c}IL]"
    return f"[{c}{c.upper()}]"


def _spans(word: re.Pattern[str], text: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    match = word.search(text)
    while match is not None:
        found.append(match.span())
        match = word.search(text, match.start() + 1)
    return found


def _chained(words: tuple[re.Pattern[str], ...], text: str) -> bool:
    ends = sorted(end for _, end in _spans(words[0], text))
    if len(words) == 1 or not ends:
        return bool(ends)
    gap_from = [0] * (len(text) + 1)
    for i in range(1, len(text) + 1):
        gap_from[i] = gap_from[i - 1] if text[i - 1] in _GAP else i
    for word in words[1:]:
        reached: list[int] = []
        for start, end in _spans(word, text):
            # an earlier word ends inside the gap run that reaches this start, before it
            first = bisect.bisect_left(ends, gap_from[start])
            if first < len(ends) and ends[first] < start:
                reached.append(end)
        ends = sorted(reached)
        if not ends:
            return False
    return True


def holds(text: str, pattern: str) -> bool:
    """Whether the text reads as the pattern in either case order."""
    words = _compiled(pattern)
    return any(_chained(words, form) for form in cells(text))


def camel_split(identifier: str) -> str:
    """An automation id with a space at each Unicode case change: a lower letter or digit before
    a capital ("btn" then a Cyrillic capital), and the last capital of a run before a capital and
    a small letter (BTNDelete)."""
    out: list[str] = []
    for index, ch in enumerate(identifier):
        if index and ch.isupper():
            before = identifier[index - 1]
            after = identifier[index + 1] if index + 1 < len(identifier) else ""
            if before.islower() or before.isdecimal() or (before.isupper() and after.islower()):
                out.append(" ")
        out.append(ch)
    return "".join(out)
