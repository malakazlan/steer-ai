"""The MTA act comparison's pure part: the per-arm summary and the rows that differ."""

from __future__ import annotations

from steerai.probes.mta_act import STEPS, compare


def test_the_summary_counts_steps_and_rows_trusted_in_every_run() -> None:
    rows = [
        {"apartment": "sta", "steps": [{"ok": True}, {"ok": True}], "trusted_rows": ["g02", "g03"]},
        {"apartment": "sta", "steps": [{"ok": True}, {"ok": False}], "trusted_rows": ["g02"]},
        {"apartment": "mta", "steps": [{"ok": True}, {"ok": True}], "trusted_rows": ["g02", "g03"]},
    ]
    summary = compare(rows)
    assert summary["sta"] == {
        "runs": 2, "steps_ok": 3, "steps": 4, "trusted_in_every_run": ["g02"]
    }  # fmt: skip
    assert summary["mta"] == {
        "runs": 1, "steps_ok": 2, "steps": 2, "trusted_in_every_run": ["g02", "g03"]
    }  # fmt: skip
    assert summary["differ"] == ["g03"]


def test_an_arm_with_no_runs_trusts_nothing() -> None:
    assert compare([])["differ"] == []


def test_the_steps_are_the_galleries_widget_rows_and_name_no_application() -> None:
    assert len(STEPS) >= 10
    assert all(row.startswith("g") for row, *_ in STEPS)
