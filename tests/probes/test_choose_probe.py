"""The g12 diagnosis probe's pure parts: the ComboBoxes read before acting, the one picked for
the v2 dispatch, and the v1 and v2 results kept whole (no live call here)."""

from __future__ import annotations

from typing import Any

from steerai.probes.choose_probe import combo_rows, pick_combo, step_record
from tests.loop2.world_fakes import element

COUNTRY = element(20, "ComboBox", "Country", (9, 20), 1)
COUNTRY_LONG = element(21, "ComboBox", "Country Choose…", (9, 21), 1)
ZONE = element(22, "ComboBox", "Time zone", (9, 22), 1)
LABEL = element(23, "Text", "Country", (9, 23), 1)


def test_combo_rows_lists_only_comboboxes_with_their_live_values() -> None:
    values = {20: "Choose…", 22: None}
    rows = combo_rows([COUNTRY, LABEL, ZONE], lambda e: values[e.id])
    assert rows == [
        {"id": 20, "name": "Country", "automation_id": "", "value": "Choose…",
         "runtime_id": [9, 20]},
        {"id": 22, "name": "Time zone", "automation_id": "", "value": None,
         "runtime_id": [9, 22]},
    ]  # fmt: skip


def test_an_exact_name_is_picked_first() -> None:
    assert pick_combo([COUNTRY_LONG, COUNTRY], "Country") == (COUNTRY, "exact")


def test_a_single_prefix_match_is_picked_and_said_so() -> None:
    assert pick_combo([COUNTRY_LONG, ZONE], "Country") == (COUNTRY_LONG, "prefix")


def test_no_match_or_two_prefix_matches_picks_nothing() -> None:
    other = element(24, "ComboBox", "Country of birth", (9, 24), 1)
    assert pick_combo([ZONE], "Country") == (None, None)
    assert pick_combo([COUNTRY_LONG, other], "Country") == (None, None)
    assert pick_combo([LABEL], "Country") == (None, None)


def test_step_record_keeps_gate_codes_action_and_evidence() -> None:
    raw: dict[str, Any] = {  # Any: v1's StepResult.to_dict()
        "step_id": "s1",
        "ok": False,
        "gate": "allow",
        "action": {"verb": "choose", "delivery": "not_delivered", "ok": False,
                   "code": "action_unsupported", "steps": [], "evidence": {"why": "x"}},
        "verify_code": None,
        "label_kind": None,
        "code": "action_unsupported",
        "latency_ms": {"gate": 1.0},
        "evidence": {"risk": "R1", "reasons": ["a", "b"]},
    }  # fmt: skip
    kept = step_record(raw)
    assert kept["ok"] is False
    assert kept["gate"] == "allow"
    assert kept["code"] == "action_unsupported"
    assert kept["action"] == raw["action"]
    assert kept["evidence"] == {"risk": "R1", "reasons": "['a', 'b']"}


def test_step_record_cuts_each_evidence_value_to_200_characters() -> None:
    kept = step_record({"ok": False, "evidence": {"long": "x" * 500}})
    assert kept["evidence"] == {"long": "x" * 200}
    assert kept["action"] is None
