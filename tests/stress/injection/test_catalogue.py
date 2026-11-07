"""The catalogue's own shape: one record per (id, case), gaps named by issue, the signature
deciding by code whether a dispatch is the injected action."""

from __future__ import annotations

import re

from tests.stress.injection.catalogue import (
    CATALOGUE,
    FIXTURES,
    ZWSP,
    Signature,
    fixture,
    pinned,
    sentinels,
)

PINNED = {
    "IJ-01",
    "IJ-02",
    "IJ-03",
    "IJ-04",
    "IJ-05",
    "IJ-06",
    "IJ-11",
    "IJ-12",
    "IJ-13",
    "IJ-14",
    "IJ-15",
    "IJ-16",
    "IJ-17",
} | {
    "IJ-18",
    "IJ-19",
    "IJ-20",
    "IJ-21",
    "IJ-23",
    "IJ-28",
    "IJ-34",
    "IJ-36",
}
GAPS = {
    "IJ-02": "#82",
    "IJ-10": "#86",
    "IJ-24": "#87",
    "IJ-37": "#87",
    "IJ-21": "#92",
}


def test_every_fixture_has_a_unique_key_and_a_known_gap_form() -> None:
    keys = [attack.key for attack in FIXTURES]
    assert len(keys) == len(set(keys))
    for attack in FIXTURES:
        assert re.fullmatch(r"IJ-\d{2}", attack.id)
        assert attack.gap is None or re.fullmatch(r"#\d+", attack.gap)


def test_the_catalogue_covers_the_pinned_defences_and_the_filed_gaps() -> None:
    assert {ij for ij in CATALOGUE if pinned(ij)} == PINNED
    filed = {a.id: a.gap for a in FIXTURES if a.gap is not None}
    assert filed == GAPS


def test_a_signature_matches_by_verb_role_and_folded_name_only() -> None:
    send = Signature("click", "Button", "Send")
    assert send.matches("click", "Button", "  send ")
    assert not send.matches("click", "Link", "Send")
    assert not send.matches("type", "Button", "Send")
    assert not send.matches("click", "Button", f"Se{ZWSP}nd"), "a lookalike is another name"


def test_answers_are_not_trace_sentinels_but_page_text_is() -> None:
    assert fixture("IJ-14", "bare_yes").payload not in sentinels()
    assert fixture("IJ-11", "ignore").payload in sentinels()
