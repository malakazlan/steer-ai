"""[V2.63] memory: stage 1 (local fixtures only) runs with a 3072 MB floor, recorded on every
row, with the working-set sampler on; stage 2 keeps 4096 MB. The sampler takes a reading before
and after the child (and every `every_s` between); a reading that raises is skipped."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.loop2 import stage1
from steerai.loop2.stage1 import (
    STAGE1_FLOOR_MB,
    RunRecord,
    accept,
    classify,
    precheck,
    run_sampled,
    run_task_runs,
)
from tests.loop2.test_stage1 import DONE, _final

MEMORY = {
    "samples": 2,
    "peak_tree_ws_mb": 1100.0,
    "peak_tree_sum_of_peaks_mb": 1180.0,
    "min_free_mb": 3300,
    "max_processes": 14,
}


def test_stage_1_runs_on_a_3072_mb_floor_and_stage_2_keeps_4096() -> None:
    assert (STAGE1_FLOOR_MB, stage1.FLOOR_MB) == (3072, 4096)
    floor = STAGE1_FLOOR_MB
    assert precheck(free_mb=lambda: 3500, idle_s=lambda: 5.0, floor_mb=floor) is None
    assert precheck(free_mb=lambda: 3071, idle_s=lambda: 5.0, floor_mb=floor) == "low_memory"
    assert precheck(free_mb=lambda: 3500, idle_s=lambda: 5.0) == "low_memory"  # default 4096


def test_every_row_records_the_floor() -> None:
    rows: list[dict[str, Any]] = []
    ran = RunRecord("a", 2, _final(), True, True, True, 0)
    outcomes: list[RunRecord | str] = ["low_memory", ran]

    def attempt(run: int) -> RunRecord | str:
        return outcomes[run - 1]

    run_task_runs(DONE, runs=1, attempt=attempt, emit=lambda r: rows.append(dict(r)),
                  row={"floor_mb": STAGE1_FLOOR_MB})  # fmt: skip
    assert [r["floor_mb"] for r in rows] == [3072, 3072]


def test_the_sampler_reads_before_and_after_the_child_and_summarises() -> None:
    readings = iter([(3900, (900.0, 950.0, 12)), (3300, (1100.0, 1180.0, 14))])

    def read() -> tuple[int, tuple[float, float, int]]:
        return next(readings)

    result, memory = run_sampled(lambda: "ran", read=read, every_s=3600.0)
    assert result == "ran"
    assert memory == MEMORY


def test_a_reading_that_raises_is_skipped_never_guessed() -> None:
    failed = [False]

    def read() -> tuple[int, tuple[float, float, int]]:
        if not failed[0]:
            failed[0] = True
            raise OSError("gone")
        return 3300, (1000.0, 1000.0, 9)

    _, memory = run_sampled(lambda: None, read=read, every_s=3600.0)
    assert memory["samples"] == 1


def test_the_childs_failure_is_its_own_and_ends_the_sampling() -> None:
    def boom() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_sampled(boom, read=lambda: (4000, (1.0, 1.0, 1)), every_s=0.01)


def test_the_runs_memory_reaches_its_row_and_the_summary() -> None:
    record = RunRecord("a", 1, _final(), True, True, True, 0, memory=MEMORY)
    assert classify(DONE, record).memory == MEMORY
    rows: list[dict[str, Any]] = []
    runs, _ = run_task_runs(DONE, runs=3, attempt=lambda _r: record,
                            emit=lambda r: rows.append(dict(r)))  # fmt: skip
    assert rows[0]["memory"] == MEMORY
    summary = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t",
                     floor_mb=STAGE1_FLOOR_MB)  # fmt: skip
    assert summary.report["memory"] == {
        "floor_mb": 3072, "runs_sampled": 3, "peak_tree_ws_mb_max": 1100.0,
        "min_free_mb_min": 3300,
    }  # fmt: skip
