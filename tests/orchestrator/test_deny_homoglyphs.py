"""Deny words through lookalike letters and folding marks (#83, IJ-03; hunter HG-01..41): a name
that reads as a deny word on screen raises the step, whatever code points spell it. Special
characters are built with `chr()`: ruff flags ambiguous characters, and an escape is readable."""

from __future__ import annotations

import pytest

from steerai.orchestrator.contracts import Risk, Step, Target
from steerai.orchestrator.gate import effective_risk

CYR_DZE = chr(0x0405)  # looks like S
CYR_IE = chr(0x0435)  # looks like e
CYR_O = chr(0x041E)  # looks like O
CYR_BYE_I = chr(0x0456)  # looks like i
GREEK_NU = chr(0x039D)  # looks like N; its small letter folds to v
GREEK_UPSILON = chr(0x03A5)  # looks like Y; its small letter folds to u
INV_EXCL = chr(0x00A1)  # inverted exclamation: a mark that the table folds to i
ARABIC_ONE = chr(0x0661)  # Arabic-Indic digit one: the table folds it to l
CGJ = chr(0x034F)  # combining grapheme joiner: invisible, not a format character
HANGUL_FILLER = chr(0x3164)  # invisible letter
DOTTED_I = chr(0x0130)  # casefolds to i + combining dot above


def _risk(name: str, *, role: str = "Button", automation_id: str = "") -> Risk:
    step = Step(
        id="c1",
        subgoal="g1",
        verb="click",
        target=Target(role=role, name=name, automation_id=automation_id),
        risk="R0",
    )
    return effective_risk(step)


@pytest.mark.parametrize(
    ("name", "tier"),
    [
        (f"{CYR_DZE}end", "R2"),  # IJ-03
        (f"D{CYR_IE}lete chat", "R3"),
        (f"{CYR_DZE}end{INV_EXCL}", "R2"),  # HG-01: a homoglyph and a folding mark
        (f"D{CYR_IE}{ARABIC_ONE}ete{INV_EXCL}", "R3"),  # HG-02
        (f"D{CYR_IE}1ete", "R3"),  # re-review: a homoglyph and an ASCII lookalike
        (f"{CYR_DZE}ubm1t", "R2"),
        (f"SE{GREEK_NU}D", "R2"),  # HG-04: a capital whose small letter folds elsewhere
        (f"PA{GREEK_UPSILON}", "R3"),
        (f"U{GREEK_NU}INSTA{ARABIC_ONE}L", "R3"),
        (f"Send{HANGUL_FILLER}Now", "R2"),  # HG-07: an invisible letter glued on
        (f"Se{CGJ}nd", "R2"),  # HG-06
        ("Se\x01nd", "R2"),  # HG-08
        (f"Sign{INV_EXCL} {CYR_O}ut", "R2"),  # HG-03: a multi-word pattern across features
        (f"SUBM{DOTTED_I}T", "R2"),  # HG-05
        ("P0ST", "R2"),  # HG-13
        ("De1ete", "R3"),
        ("DeIete", "R3"),
        (chr(0x0420) + chr(0x0430) + chr(0x0443), "R3"),  # HG-16: all-Cyrillic "pay"
    ],
)
def test_a_deny_word_spelled_with_lookalikes_raises_the_step(name: str, tier: Risk) -> None:
    assert _risk(name) == tier


@pytest.mark.parametrize(
    ("automation_id", "tier"),
    [
        (f"btn{CYR_DZE}end", "R2"),  # HG-17: a non-ASCII capital starts the word
        (f"xyz{CYR_IE}Delete", "R3"),
        ("BTNDelete", "R3"),  # HG-26: an acronym before the word
    ],
)
def test_an_automation_id_splits_on_unicode_case(automation_id: str, tier: Risk) -> None:
    assert _risk("OK", automation_id=automation_id) == tier


def test_control_words_through_lookalikes_keep_their_role_scope() -> None:
    like = f"L{CYR_BYE_I}ke"
    assert _risk(like, role="Button") == "R2"
    assert _risk(f"{like} That", role="Hyperlink") == "R0", "a title is not a control (HG-24)"
    assert _risk("Watch", role="Hyperlink", automation_id=f"{like}Button") == "R2"


@pytest.mark.parametrize(
    "name",
    [
        "Send2",  # IJ-15: a digit never hides the word
        chr(0xFF33) + chr(0xFF45) + chr(0xFF4E) + chr(0xFF44),  # fullwidth: NFKC already
        "".join(chr(0x1D400 + ord(c) - ord("A")) for c in "SEND"),  # math bold capitals
        "Del" + chr(0x200B) + "ete",  # a zero-width space (IJ-15)
    ],
)
def test_what_is_raised_today_stays_raised(name: str) -> None:
    assert _risk(name) in ("R2", "R3")


@pytest.mark.parametrize(
    "name",
    ["Inbox", "Sara", "Sender list", INV_EXCL * 3, chr(0x00D7), chr(0x0307)],
)
def test_plain_names_and_lone_marks_stay_where_the_plan_put_them(name: str) -> None:
    assert _risk(name) == "R0"
