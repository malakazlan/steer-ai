"""Person matching (design 5.8 [v0.9], [v1.5]): which rows name the contact a plan means.

The rule is deterministic code, never a model: a parameter matches an element when the
element's name part (the text before the first separator) equals the parameter, or starts with
it as a whole token, both in the comparable form: NFKC, format characters removed, case-folded
(`steerai.verify.predicates.comparable`), then confusable letters folded to their Latin
prototype through a table generated from Unicode UTS #39 `confusables.txt` and vendored below
with its Unicode version. A confusable twin therefore matches on purpose: it must be asked
about, never silently excluded or silently chosen.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence

from steerai.verify.confusables import CONFUSABLE_TO_LATIN
from steerai.verify.predicates import comparable as _nfkc_casefold
from steerai.world.elements import Element

MAX_CANDIDATES = 12
"""More matches than this is not an ask a person can answer (PT-26): `ambiguous_target`."""
_SEPARATORS = (" \u00b7 ", "\u00b7", " - ", " \u2013 ", " \u2014 ")
_TOKEN_BOUNDARIES = frozenset({" ", "\u00b7", "_", "("})


_FOLD = str.maketrans(CONFUSABLE_TO_LATIN)


def comparable(text: str) -> str:
    return _nfkc_casefold(text).translate(_FOLD)


def name_part(name: str) -> str:
    """The text before the first separator, stripped; the whole name when there is none."""
    cut = len(name)
    for separator in _SEPARATORS:
        index = name.find(separator)
        if index != -1:
            cut = min(cut, index)
    return name[:cut].strip()


def preview(name: str) -> str:
    """The text after the name part and its separator, stripped; empty when none."""
    part = name_part(name)
    rest = name.strip()[len(part) :] if name.strip().startswith(part) else ""
    for separator in _SEPARATORS:
        if rest.startswith(separator):
            rest = rest[len(separator) :]
            break
    return rest.strip(" \u00b7-\u2013\u2014")


def matches(parameter: str, name: str, *, wanted: str | None = None) -> bool:
    """`wanted` is the parameter's comparable form when the caller has it already."""
    wanted = comparable(parameter).strip() if wanted is None else wanted
    if not wanted:
        return False
    part = comparable(name_part(name)).strip()
    if part == wanted:
        return True
    if not part.startswith(wanted):
        return False
    return part[len(wanted)] in _TOKEN_BOUNDARIES


def lookalike_only(parameter: str, name: str) -> bool:
    """True when the name matches the parameter only through confusable folding (PT-01): the
    comparable forms agree while the NFKC, case-folded forms differ. Such a match is never a
    silent resolve; it always asks."""
    if not matches(parameter, name):
        return False
    plain_param = _nfkc_casefold(parameter).strip()
    plain_part = _nfkc_casefold(name_part(name)).strip()
    if plain_part == plain_param:
        return False
    return not (
        plain_part.startswith(plain_param) and plain_part[len(plain_param)] in _TOKEN_BOUNDARIES
    )


def _script(ch: str) -> str | None:
    if not ch.isalpha():
        return None
    try:
        return unicodedata.name(ch).split(" ", 1)[0]
    except ValueError:
        return None


def mixed_script(name: str) -> bool:
    """Letters from more than one script in the name part (PT-13), computed from the character
    names' first word (LATIN, CYRILLIC, GREEK, ...), never from a table."""
    scripts = {s for s in (_script(ch) for ch in name_part(name)) if s is not None}
    return len(scripts) > 1


def candidates(elements: Iterable[Element], role: str, parameter: str) -> Sequence[Element]:
    """The elements of the role whose name part names the parameter, in document order."""
    wanted = comparable(parameter).strip()
    return [e for e in elements if e.role == role and matches(parameter, e.name, wanted=wanted)]
