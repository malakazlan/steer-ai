"""`gate.may_read` (#314, [V2.32], [V2.35] (1)): a cheap, one-sided prefilter. It is False only
when no deny, control, consent or billing pattern and no price can read any text of the facts,
so `deny_tier_of` and `billing_reads` would read nothing. It may say "check this one" when it
need not; it may never say "no hit" where they would read. Locked here three ways: on the deny
grid's names, groups and ids under every role and form verb; by a seeded fuzz of lookalike,
invisible and near-miss text (adapted from the principal's #319 fuzz script); and structurally,
on the pattern classes it is built from."""

from __future__ import annotations

import itertools
import random
from collections.abc import Mapping

import pytest

from steerai.orchestrator import gate
from steerai.orchestrator.gate import DenyFacts, billing_reads, deny_tier_of, may_read
from steerai.verify.confusables import ASCII_LOOKALIKES, CONFUSABLE_TO_LATIN
from steerai.verify.predicates import comparable
from tests.orchestrator.deny_grid import (
    ALL_ROLES,
    BOUND_NAMES,
    GROUP_NAMES,
    GROUPED_NAMES,
    ID_FORMS,
    OPTION_NAMES,
    ROLE_IDS,
    SAMPLE_NAMES,
    WORD_NAMES,
)

NAMES = sorted(
    {n for n in (*WORD_NAMES, *SAMPLE_NAMES, *GROUPED_NAMES, *OPTION_NAMES, *BOUND_NAMES) if n}
    | {""}
)
GROUPS = sorted({g for g in GROUP_NAMES if g} | {""})
IDS = sorted({*ID_FORMS, *ROLE_IDS, ""})
FORM_VERBS = ("type", "choose", "click", "select", "check")
"""The verbs the form facts read with (`loop2.form.fitting_verbs`)."""


def facts(name: str = "", **kw: object) -> DenyFacts:
    return DenyFacts(verb="click", role="Button", name=name, **kw)  # type: ignore[arg-type]  # test kwargs


# ---- the grid lock -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_no_reading_hides_behind_a_name(name: str) -> None:
    for group, automation_id in itertools.product(GROUPS, IDS):
        if may_read(facts(name, automation_id=automation_id, group_name=group or None)):
            continue  # it reads the texts only: the verb and the role cannot change its answer
        for verb, role in itertools.product(FORM_VERBS, ALL_ROLES):
            read = DenyFacts(
                verb=verb,
                role=role,
                name=name,
                automation_id=automation_id,
                group_name=group or None,
            )
            assert deny_tier_of(read) == "R0", read
            assert billing_reads(read) is False, read


def test_the_prefilter_lets_through_what_the_gate_reads() -> None:
    assert may_read(facts("Delete"))
    assert may_read(facts("Pro $10/mo"))
    assert may_read(facts(automation_id="btnDelete"))
    assert may_read(facts("Subscription"))


def test_the_prefilter_rules_out_plain_text() -> None:
    assert not may_read(facts("Search", group_name="Results for red shoes"))
    assert not may_read(facts(automation_id="searchInput"))
    assert not may_read(facts())


# ---- S1-03: every text of the facts is read; no caller can drop one ----------------------------


@pytest.mark.parametrize(
    "read",
    [
        DenyFacts(verb="choose", role="ComboBox", name="Plan", option="Delete account"),
        DenyFacts(verb="click", role="Button", name="Remove", bound_name="Delete Me"),
        DenyFacts(verb="click", role="Button", name="Go", element_ids=("btnDeleteAccount",)),
        DenyFacts(verb="check", role="RadioButton", name="Yes", group_name="Pay $120 a year"),
    ],
    ids=["option", "bound_name", "element_ids", "group_name"],
)
def test_every_text_of_the_facts_is_read(read: DenyFacts) -> None:
    assert deny_tier_of(read) != "R0" or billing_reads(read)
    assert may_read(read)


# ---- S1-01: the prefilter is built from every pattern class -------------------------------------


def test_every_pattern_class_of_the_gate_feeds_the_prefilter() -> None:
    declared = {
        name
        for name, value in vars(gate).items()
        if name.endswith("_PATTERNS") and isinstance(value, Mapping)
    }
    shared = {name for name, value in vars(gate).items() if value in gate.PATTERN_CLASSES}
    assert declared == shared
    for patterns in gate.PATTERN_CLASSES:
        for pattern in patterns:
            assert may_read(facts(pattern)), pattern


# ---- S1-04: the pattern maps cannot change under the caches ------------------------------------


@pytest.mark.parametrize("patterns", gate.PATTERN_CLASSES)
def test_the_pattern_maps_are_read_only(patterns: Mapping[str, str]) -> None:
    with pytest.raises(TypeError):
        patterns["zzz"] = "R3"  # type: ignore[index]  # the point: it must refuse


# ---- S1-02: a seeded fuzz of lookalike, invisible and near-miss text ----------------------------

PATTERNS = [p for patterns in gate.PATTERN_CLASSES for p in patterns]
_LOOKALIKE: dict[str, list[str]] = {}
for _source, _letter in (*CONFUSABLE_TO_LATIN.items(), *ASCII_LOOKALIKES.items()):
    _LOOKALIKE.setdefault(_letter, []).append(_source)
INVISIBLE = [chr(c) for c in (0x200B, 0x200D, 0x034F, 0x3164, 0x202E, 0x202C, 0x00AD, 0x2060)]
SEPARATORS = [" ", "-", "_", ".", "'", chr(0x2019), chr(0x02BC), "/", chr(0x00A0), "", "1"]
PRICES = ["$", chr(0x20AC), chr(0x00A3), chr(0x20B9), chr(0xFF04), chr(0xFE69), "/mo", "9"]
SEED = 319
CASES = 4000


def _word(rng: random.Random, word: str) -> str:
    out: list[str] = []
    for c in word:
        r = rng.random()
        if r < 0.15 and c in _LOOKALIKE:
            out.append(rng.choice(_LOOKALIKE[c]))
        elif r < 0.25:
            out.append(c.upper())
        elif r < 0.30:
            out.append(rng.choice(INVISIBLE) + c)
        elif r < 0.33:
            continue
        elif r < 0.36:
            out.append(c + rng.choice("abcxyz"))
        else:
            out.append(c)
    return "".join(out)


def _text(rng: random.Random) -> str:
    parts: list[str] = []
    for _ in range(rng.randint(1, 4)):
        r = rng.random()
        if r < 0.6:
            words = rng.choice(PATTERNS).split()
            if len(words) > 1 and rng.random() < 0.3:
                words = words[1:]
            parts.append(rng.choice(SEPARATORS).join(_word(rng, w) for w in words))
        elif r < 0.8:
            parts.append(rng.choice(PRICES))
        else:
            parts.append(rng.choice(["Search", "Kyoto", "Results", "btn", "Plan", "Yes"]))
    text = rng.choice(SEPARATORS).join(parts)
    return text if rng.random() < 0.5 else text.replace(" ", "")


def _reads_any(texts: list[str], automation_id: str) -> bool:
    for text in texts:
        if text and gate._BILLED.search(comparable(text)):
            return True
        if text and any(gate._reads(text, p) for p in PATTERNS):
            return True
    return bool(automation_id) and any(gate._reads_id(automation_id, p) for p in PATTERNS)


def test_no_fuzzed_text_reads_where_the_prefilter_says_no() -> None:
    rng = random.Random(SEED)  # noqa: S311 - a seeded fuzz, reproducible; nothing secret
    ruled_out = 0
    for _ in range(CASES):
        name = _text(rng)
        group = _text(rng) if rng.random() < 0.3 else ""
        automation_id = _text(rng) if rng.random() < 0.5 else ""
        read = facts(name, automation_id=automation_id, group_name=group or None)
        if may_read(read):
            continue
        ruled_out += 1
        assert not _reads_any([name, group], automation_id), (name, group, automation_id)
    assert ruled_out > CASES // 10, "the fuzz must exercise the 'no' side"
