"""Person matching edges from the hunt (PT-01/07/13/21/24/34): lookalike-only matches, mixed
scripts, boundaries, separators, and the codec's rule for person parameters."""

from __future__ import annotations

import json

import pytest

from steerai.orchestrator.plan_io import PlanError, plan_from_json
from steerai.world.people import (
    MAX_CANDIDATES,
    comparable,
    lookalike_only,
    matches,
    mixed_script,
    name_part,
)
from tests.orchestrator.test_plan_io import _doc

CYR_A = chr(0x0430)  # Cyrillic small a
MID = chr(0xB7)


def test_a_match_that_exists_only_through_folding_is_flagged() -> None:
    assert matches("Amir", f"{CYR_A}mir {MID} hi") is True
    assert lookalike_only("Amir", f"{CYR_A}mir {MID} hi") is True
    assert lookalike_only("Amir", f"Amir {MID} hi") is False
    assert lookalike_only("amir", f"AMIR {MID} hi") is False, "case is not a lookalike"
    assert lookalike_only("Amir", chr(0xFF21) + f"mir {MID} hi") is False, (
        "NFKC width is not a lookalike"
    )
    assert lookalike_only("Amir", f"Sara {MID} hi") is False, "no match at all"


def test_mixed_script_detects_letters_from_two_scripts_in_the_name_part() -> None:
    assert mixed_script(f"{CYR_A}mir {MID} hi") is True
    assert mixed_script(f"{CYR_A}мир {MID} hi") is False, "all Cyrillic"
    assert mixed_script(f"Amir {MID} {CYR_A}") is False, "the preview does not count"
    assert mixed_script("Amir 123 (work)") is False
    assert mixed_script("") is False


@pytest.mark.parametrize(
    ("parameter", "name", "expected"),
    [
        ("Amir", "Amir's " + MID + " hi", False),
        ("Amir", "Amir\U0001f525", False),
        ("Amir", "Amir \U0001f525", True),
        ("Amir", "Amir-ul-Haq", False),
        ("Amir", "Amir. " + MID + " hi", False),
        ("Amir", "Amir: hi", False),
        ("Khan", "Amir Khan", False),
        ("Amir Khan", "Amir Khan, Sara " + MID + " now", False),
        ("A", "A " + MID + " hi", True),
        ("A", "Amir", False),
        ("Amir", "Dr. Amir " + MID + " hi", False),
        (MID, "Amir " + MID + " hi", False),
        ("Amir", MID + " Amir " + MID + " 2h", False),
        ("Amir", "Jean-Luc " + MID + " hi", False),
        ("Jean-Luc", "Jean-Luc " + MID + " hi", True),
        ("Amir", "Amir - Work " + MID + " hi", True),
    ],
)
def test_boundaries_and_separator_positions(parameter: str, name: str, expected: bool) -> None:
    assert matches(parameter, name) is expected


def test_the_earliest_separator_wins_and_a_hyphen_needs_spaces() -> None:
    assert name_part("Amir - Work " + MID + " hi") == "Amir"
    assert name_part("Jean-Luc " + MID + " hi") == "Jean-Luc"
    assert name_part(MID) == ""


def test_the_candidate_cap_is_twelve() -> None:
    assert MAX_CANDIDATES == 12


@pytest.mark.parametrize("name", ["Amir " + MID + " ok", "Amir - Work", MID + " Amir", "   ", MID])
def test_the_codec_refuses_a_person_parameter_with_a_separator_or_an_empty_name_part(
    name: str,
) -> None:
    doc = _doc()
    doc["steps"][0]["target"] = {"role": "Hyperlink", "name": name, "match": "person"}
    with pytest.raises(PlanError) as info:
        plan_from_json(json.dumps(doc))
    assert info.value.path == "$.steps[0].target.name"


def test_the_codec_keeps_an_exact_name_with_a_separator() -> None:
    doc = _doc()
    doc["steps"][0]["verb"] = "click"  # a focus targets only a field (#82)
    doc["steps"][0]["target"] = {
        "role": "Hyperlink",
        "name": "Amir " + MID + " hi bro " + MID + " 2h",
    }
    plan_from_json(json.dumps(doc))


CYR_SMALL_I = chr(
    0x0456
)  # Cyrillic small byelorussian-ukrainian i: UTS #39 says i, its capital says l


def test_a_letters_own_line_beats_its_capitals_fold_in_the_table() -> None:
    assert comparable("Am" + CYR_SMALL_I + "r") == "amir"
    assert lookalike_only("Amir", "Am" + CYR_SMALL_I + "r " + MID + " hi") is True
    assert comparable("Am" + chr(0x0406) + "r") == "amir", (
        "a capital folds through its lowercase line"
    )
