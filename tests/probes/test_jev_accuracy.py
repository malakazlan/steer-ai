"""Jev element-accuracy probe: dataset loading, label resolution, and scoring (pure parts)."""

import pytest

from steerai.probes.element_list import Element
from steerai.probes.jev_accuracy import (
    Case,
    Outcome,
    load_cases,
    resolve_expected_id,
    summarize,
)

BOX = (0, 0, 10, 10)


def _el(idx: int, role: str, name: str) -> Element:
    return Element(id=idx, depth=1, role=role, name=name, box=BOX)


ELEMENTS = [
    _el(1, "Hyperlink", "Amir · hi bro · 2h"),
    _el(2, "Hyperlink", "Amir Khan · ok · 3d"),
    _el(3, "Button", "Send"),
    _el(4, "Edit", "Message"),
]


def test_load_cases_reads_bundled_dataset_with_negatives() -> None:
    cases = load_cases()
    assert len(cases) == 50
    assert sum(1 for c in cases if c.role is None) == 5
    assert all(isinstance(c, Case) for c in cases)


def test_resolve_expected_id_matches_role_and_name_exactly() -> None:
    assert resolve_expected_id(Case("x", "Hyperlink", "Amir Khan · ok · 3d"), ELEMENTS) == 2
    assert resolve_expected_id(Case("x", "Button", "Send"), ELEMENTS) == 3


def test_resolve_expected_id_none_for_negative_case() -> None:
    assert resolve_expected_id(Case("x", None, None), ELEMENTS) is None


def test_resolve_expected_id_raises_when_label_missing_or_ambiguous() -> None:
    with pytest.raises(LookupError, match="no element"):
        resolve_expected_id(Case("x", "Button", "Missing"), ELEMENTS)
    duplicated = [*ELEMENTS, _el(5, "Button", "Send")]
    with pytest.raises(LookupError, match="2 elements"):
        resolve_expected_id(Case("x", "Button", "Send"), duplicated)


def test_summarize_reports_accuracy_and_gated_coverage() -> None:
    outcomes = [
        Outcome("a", expected=2, chosen=2, confidence=0.9, margin=0.8, latency_ms=100),
        Outcome("b", expected=3, chosen=4, confidence=0.9, margin=0.7, latency_ms=120),
        Outcome("c", expected=None, chosen=None, confidence=0.5, margin=0.3, latency_ms=90),
        Outcome("d", expected=4, chosen=4, confidence=0.7, margin=0.4, latency_ms=110),
    ]
    summary = summarize(outcomes, gate=0.85)
    assert summary["n"] == 4
    assert summary["accuracy"] == pytest.approx(0.75)
    assert summary["gated_coverage"] == pytest.approx(0.5)  # 2 of 4 at or above the gate
    assert summary["gated_accuracy"] == pytest.approx(0.5)  # 1 of those 2 correct
    assert summary["latency_p50_ms"] == 100  # nearest rank 2 of [90, 100, 110, 120]
