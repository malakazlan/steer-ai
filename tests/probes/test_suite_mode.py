"""[V2.16]: live suites run chunked by default (`--chunk-size 1`), `--in-process` opts out for
debugging; every row records its run mode; aborted and refused rows are infrastructure, never a
Steer outcome; on the sealed H7 set a task that hit the wall-time cap counts as not done."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from steerai.probes import task_suite as suite


def _rows(out: Path) -> list[dict[str, Any]]:  # Any: JSON rows
    return [json.loads(line) for line in out.read_text("utf-8").splitlines() if line.strip()]


def _never(*_: object) -> int:
    pytest.fail("this run must stay in process")


def _one_run(
    task: Any, number: int, _planner: object, _kept: object = None, *, tmp_root: object = None
) -> dict[str, Any]:  # Any: the suite's task and JSON row
    del tmp_root
    return {"task": task.task_id, "run": number, "passed": True, "outcome": "true_done"}


def test_a_live_run_is_chunked_one_task_per_process_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[int] = []

    def chunked(args: argparse.Namespace, units: Sequence[Any], tasks: Sequence[Any]) -> int:
        seen.append(args.chunk_size)
        return 0

    monkeypatch.setattr(suite, "_chunked", chunked)
    suite.main(["--set", "gallery", "--runs", "1", "--tasks", "g01", "--out", str(tmp_path / "o")])
    assert seen == [1]


def test_in_process_is_an_explicit_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(suite, "_chunked", _never)
    monkeypatch.setattr(suite, "free_physical_mb", lambda: 1_000_000)
    monkeypatch.setattr(
        suite,
        "one_run",
        _one_run,
    )
    out = tmp_path / "rows.jsonl"
    suite.main(
        ["--in-process", "--set", "gallery", "--runs", "1", "--tasks", "g01", "--out", str(out)]
    )
    rows = _rows(out)
    assert {r["mode"] for r in rows} == {"in_process"}, "every row, the header too"


def test_a_child_run_number_stays_in_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The chunk runner starts each child with `--run-number`; the child must not chunk again."""
    monkeypatch.setattr(suite, "_chunked", _never)
    monkeypatch.setattr(suite, "free_physical_mb", lambda: 1_000_000)
    monkeypatch.setattr(
        suite,
        "one_run",
        _one_run,
    )
    out = tmp_path / "rows.jsonl"
    suite.main(["--set", "gallery", "--run-number", "1", "--tasks", "g01", "--out", str(out)])
    assert _rows(out)[-1]["task"] == "g01"


def test_aborted_and_refused_rows_are_infrastructure_never_an_outcome() -> None:
    rows: list[dict[str, Any]] = [
        {"task": "g01", "run": 1, "passed": True, "outcome": "true_done", "mode": "chunked"},
        {"task": "g02", "run": 1, "aborted": "task_cap", "mode": "chunked"},
        {"task": "g03", "run": 1, "aborted": "interrupted", "mode": "chunked"},
        {"refused": "low_memory", "remaining": ["g04:1"], "mode": "chunked"},
    ]
    scored, line = suite.split_infrastructure(rows, set_name="gallery")
    assert [r["task"] for r in scored] == ["g01"]
    assert line == "infrastructure: aborted 2 (interrupted 1, task_cap 1), refused 1"


def test_on_h7_a_capped_task_counts_as_not_done_and_a_refused_one_does_not() -> None:
    rows: list[dict[str, Any]] = [
        {"task": "h7w1", "run": 1, "aborted": "task_cap", "mode": "chunked"},
        {"refused": "low_memory", "remaining": ["h7w2:1"], "mode": "chunked"},
    ]
    scored, _ = suite.split_infrastructure(rows, set_name="h7")
    assert scored == [
        {
            "task": "h7w1",
            "run": 1,
            "passed": False,
            "outcome": "interrupted",
            "aborted": "task_cap",
            "mode": "chunked",
        }
    ]


def test_chunked_rows_carry_the_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run_chunked(chunks: Any, **kw: Any) -> int:
        kw["write_row"]({"task": "g01", "run": 1, "passed": True, "outcome": "true_done"})
        kw["write_row"]({"task": "g02", "run": 1, "aborted": "task_cap"})
        return 0

    monkeypatch.setattr(suite, "run_chunked", fake_run_chunked)
    out = tmp_path / "rows.jsonl"
    suite.main(["--set", "gallery", "--runs", "1", "--tasks", "g01,g02", "--out", str(out)])
    assert {r["mode"] for r in _rows(out)} == {"chunked"}


def test_a_unit_test_can_never_start_a_real_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The conftest guard: a chunked main with memory to spare reaches the child start, which
    fails the test instead of launching a live task."""
    from steerai.probes import suite_chunks  # noqa: PLC0415 - the guarded module

    monkeypatch.setattr(suite_chunks, "free_physical_mb", lambda: 1_000_000)
    out = tmp_path / "rows.jsonl"
    with pytest.raises(AssertionError, match="real suite child"):
        suite.main(["--set", "gallery", "--runs", "1", "--tasks", "g01", "--out", str(out)])
