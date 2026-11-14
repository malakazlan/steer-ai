"""[V2.19] item 3: the working-set probe's pure parts, without a child process or a browser."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerai.probes import suite_runner
from steerai.probes.comparability import compare
from steerai.probes.process_tree import Sample, summarize, tree_pids
from steerai.probes.working_set import report, sample_until_exit

TABLE = [(1, 0), (10, 1), (11, 10), (12, 10), (20, 2), (21, 20), (13, 99)]


def test_the_tree_is_the_root_and_every_descendant() -> None:
    assert tree_pids(10, TABLE) == {10, 11, 12}
    assert tree_pids(1, TABLE) == {1, 10, 11, 12}


def test_an_exited_root_has_no_tree() -> None:
    assert tree_pids(77, TABLE) == set()


def test_a_reused_pid_cycle_ends_the_walk() -> None:
    assert tree_pids(5, [(5, 6), (6, 5)]) == {5, 6}


def test_summary_is_the_peaks_and_the_minimum_free() -> None:
    samples = [
        Sample(0.0, 5000, 300.0, 310.0, 2),
        Sample(0.5, 4200, 900.5, 950.0, 6),
        Sample(1.0, 4600, 700.0, 960.0, 5),
    ]
    assert summarize(samples) == {
        "samples": 3,
        "peak_tree_ws_mb": 900.5,
        "peak_tree_sum_of_peaks_mb": 960.0,
        "min_free_mb": 4200,
        "max_processes": 6,
    }


def test_no_samples_say_so() -> None:
    assert summarize([]) == {"samples": 0}


def test_sampling_runs_until_the_child_exits_and_skips_an_unreadable_reading() -> None:
    polls = iter([None, None, None, 0])
    values: list[tuple[float, float, int] | OSError] = [
        (100.0, 110.0, 1),
        OSError("gone"),
        (200.0, 210.0, 2),
        (150.0, 210.0, 2),
    ]
    reads = iter(values)
    now = iter([0.0, 0.0, 0.5, 1.0, 1.5])
    waits: list[float] = []

    def read_proc() -> tuple[float, float, int]:
        value = next(reads)
        if isinstance(value, OSError):
            raise value
        return value

    samples = sample_until_exit(
        lambda: next(polls),
        read_free=lambda: 4000,
        read_proc=read_proc,
        clock=lambda: next(now),
        wait=waits.append,
    )
    assert [s.tree_ws_mb for s in samples] == [100.0, 200.0, 150.0]
    assert waits == [0.5, 0.5, 0.5]


def test_the_first_sample_is_taken_even_when_the_child_already_exited() -> None:
    samples = sample_until_exit(
        lambda: 0,
        read_free=lambda: 4000,
        read_proc=lambda: (1.0, 2.0, 1),
        clock=lambda: 0.0,
        wait=lambda _s: None,
    )
    assert len(samples) == 1


def test_the_report_has_a_row_per_task_then_the_summary() -> None:
    rows: list[dict[str, object]] = [
        {"header": True},
        {"task": "g21", "seconds": 20.5, "outcome": "true_done"},
        {"refused": "low_memory"},
        {"task": "g22", "outcome": "error"},
    ]
    out = report([Sample(0.0, 4100, 800.0, 820.0, 4)], rows, exit_code=0)
    assert out[0] == {"task": "g21", "seconds": 20.5, "outcome": "true_done"}
    assert out[1] == {"task": "g22", "seconds": None, "outcome": "error"}
    assert out[2]["report"] == "working_set"
    assert out[2]["peak_tree_ws_mb"] == 800.0
    assert out[2]["min_free_mb"] == 4100


def test_the_suite_argv_names_the_mode() -> None:
    out = Path("o.jsonl")
    a = suite_runner.suite_argv(
        set_name="gallery", tasks=["g21", "g22"], out=out, mode="in_process", floor_mb=100
    )
    b = suite_runner.suite_argv(
        set_name="gallery", tasks=["g21", "g22"], out=out, mode="chunked", floor_mb=100
    )
    assert "--in-process" in a
    assert "--in-process" not in b
    assert a[a.index("--tasks") + 1] == "g21,g22"
    with pytest.raises(ValueError, match="unknown mode"):
        suite_runner.suite_argv(set_name="gallery", tasks=[], out=out, mode="x", floor_mb=1)


def test_rows_skip_a_line_that_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_bytes(b'{"a": 1}\nnot json\n[1]\n{"task": "g1"}\n')
    assert suite_runner.read_rows(path) == [{"a": 1}, {"task": "g1"}]
    assert suite_runner.read_rows(tmp_path / "missing") == []


def test_comparability_pairs_outcomes_and_reports_the_overhead() -> None:
    a: list[dict[str, object]] = [
        {"mode": "in_process"},
        {"task": "g21", "outcome": "true_done", "seconds": 20.0},
        {"task": "g22", "outcome": "true_done", "seconds": 30.0},
        {"task": "g23", "outcome": "true_done", "seconds": 25.0},
    ]
    b: list[dict[str, object]] = [
        {"task": "g21", "outcome": "true_done", "seconds": 21.5},
        {"task": "g22", "outcome": "false_stop", "seconds": 33.0},
        {"refused": "low_memory"},
    ]
    out = compare(a, b, in_process_wall_s=80.0, chunked_wall_s=110.0)
    assert [r["equal"] for r in out[:3]] == [True, False, None]
    assert out[0]["seconds_diff"] == 1.5
    assert out[2]["seconds_diff"] is None
    assert out[3] == {
        "report": "comparability",
        "tasks": 3,
        "equal": 1,
        "different": 1,
        "unknown": 1,
        "wall_in_process_s": 80.0,
        "wall_chunked_s": 110.0,
        "overhead_per_task_s": 10.0,
    }


def test_comparability_with_no_tasks_has_no_overhead() -> None:
    out = compare([], [], in_process_wall_s=1.0, chunked_wall_s=2.0)
    assert out[-1]["overhead_per_task_s"] is None
