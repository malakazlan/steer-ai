"""[V2.14] 4 on the task suite: the pre-flight floor before each task, chunked runs, the
one-browser slot, and cleanup on abort. Fakes only: no Chrome, no Steer run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import steerai.probes.task_suite as suite
from steerai.probes.memory_guard import (
    EXIT_LOW_MEMORY,
    FLOOR_MB,
    BrowserBusyError,
    claim_browser,
    one_browser,
    release_browser,
)
from steerai.probes.suite_chunks import Unit


class Runs:
    """A fake `one_run` that records what it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, Path | None]] = []

    def __call__(
        self,
        task: Any,  # Any: a suite Task
        number: int,
        planner: Any,  # noqa: ARG002 - Any: the one_run signature; the fake needs no planner
        kept: Path | None = None,  # noqa: ARG002 - the one_run signature
        *,
        tmp_root: Path | None = None,
    ) -> dict[str, Any]:  # Any: a suite row
        self.calls.append((task.task_id, number, tmp_root))
        return {"task": task.task_id, "run": number, "passed": False}


def _free(*readings: int) -> Any:  # Any: a reading function for monkeypatch
    left = list(readings)
    return lambda: left.pop(0) if left else 1_000_000


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> Runs:
    fake = Runs()
    monkeypatch.setattr(suite, "one_run", fake)
    monkeypatch.setitem(suite._LEVEL, "trace_level", "redacted")  # main sets it: restored after
    return fake


IN_PROCESS = ["--in-process", "--set", "gallery", "--runs", "1"]


def _rows(out: Path) -> list[dict[str, Any]]:  # Any: JSON rows
    return [json.loads(line) for line in out.read_text("utf-8").splitlines() if line.strip()]


def test_below_the_floor_the_first_task_never_starts(
    runs: Runs, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(suite, "free_physical_mb", _free(100))
    out = tmp_path / "rows.jsonl"
    code = suite.main([*IN_PROCESS, "--tasks", "g01,g02", "--out", str(out)])
    assert code == EXIT_LOW_MEMORY
    assert runs.calls == []
    refusal = _rows(out)[-1]
    assert refusal == {
        "refused": "low_memory",
        "free_mb": 100,
        "floor_mb": FLOOR_MB,
        "remaining": ["g01:1", "g02:1"],
        "mode": "in_process",
    }


def test_the_floor_is_checked_before_every_task_and_finished_rows_are_kept(
    runs: Runs, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(suite, "free_physical_mb", _free(8000, 100))
    out = tmp_path / "rows.jsonl"
    code = suite.main([*IN_PROCESS, "--tasks", "g01,g02", "--out", str(out)])
    assert code == EXIT_LOW_MEMORY
    assert [c[0] for c in runs.calls] == ["g01"]
    rows = _rows(out)
    assert rows[1]["task"] == "g01"
    assert rows[-1]["remaining"] == ["g02:1"]
    assert "below the floor" in capsys.readouterr().err


def test_the_floor_is_a_flag(runs: Runs, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(suite, "free_physical_mb", _free(100))
    out = tmp_path / "rows.jsonl"
    suite.main(
        [
            "--in-process",
            "--set",
            "gallery",
            "--runs",
            "1",
            "--tasks",
            "g01",
            "--min-free-mb",
            "50",
            "--out",
            str(out),
        ]
    )
    assert [c[0] for c in runs.calls] == ["g01"]


def test_a_run_number_runs_only_that_number_under_the_given_temp_root(
    runs: Runs, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(suite, "free_physical_mb", _free())
    out = tmp_path / "rows.jsonl"
    root = tmp_path / "chunk"
    suite.main(
        [
            "--set",
            "gallery",
            "--tasks",
            "g01",
            "--runs",
            "3",
            "--run-number",
            "2",
            "--tmp-root",
            str(root),
            "--out",
            str(out),
        ]
    )
    assert runs.calls == [("g01", 2, root)]


class Chunked:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.kwargs: dict[str, Any] = {}  # Any: run_chunked's keyword arguments
        self.chunks: list[list[Unit]] = []

    def __call__(self, chunks: list[list[Unit]], **kwargs: Any) -> int:  # Any: as recorded
        self.chunks = [list(c) for c in chunks]
        self.kwargs = kwargs
        assert Path(kwargs["tmp_parent"]).is_dir()
        return self.code


def test_chunk_size_one_runs_one_task_per_child(
    runs: Runs, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chunked = Chunked()
    monkeypatch.setattr(suite, "run_chunked", chunked)
    out = tmp_path / "rows.jsonl"
    suite.main(
        [
            "--set",
            "gallery",
            "--runs",
            "1",
            "--tasks",
            "g01,g02",
            "--chunk-size",
            "1",
            "--out",
            str(out),
        ]
    )
    assert chunked.chunks == [[Unit("g01", 1)], [Unit("g02", 1)]]
    assert chunked.kwargs["base_args"] == ["--set", "gallery"]
    assert chunked.kwargs["cap_s"] == suite.TASK_CAP_S
    assert chunked.kwargs["floor_mb"] == FLOOR_MB
    assert not Path(chunked.kwargs["tmp_parent"]).exists(), "the chunks' parent folder is removed"
    assert runs.calls == [], "the parent never runs a task itself"


def test_a_task_cap_alone_implies_one_task_per_child(
    runs: Runs, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chunked = Chunked()
    monkeypatch.setattr(suite, "run_chunked", chunked)
    out = tmp_path / "rows.jsonl"
    suite.main(
        [
            "--set",
            "gallery",
            "--runs",
            "1",
            "--tasks",
            "g01,g02",
            "--task-cap-s",
            "90",
            "--out",
            str(out),
        ]
    )
    assert len(chunked.chunks) == 2
    assert chunked.kwargs["cap_s"] == 90.0


def test_a_chunked_low_memory_refusal_is_the_exit_code(
    runs: Runs, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(suite, "run_chunked", Chunked(EXIT_LOW_MEMORY))
    out = tmp_path / "rows.jsonl"
    code = suite.main(
        [
            "--set",
            "gallery",
            "--runs",
            "1",
            "--tasks",
            "g01",
            "--chunk-size",
            "1",
            "--out",
            str(out),
        ]
    )
    assert code == EXIT_LOW_MEMORY


# ---- cleanup on abort --------------------------------------------------------------------------


def test_one_run_removes_its_temp_folder_on_an_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(suite, "wait_until_idle", lambda: None)

    def interrupted(*args: Any) -> dict[str, Any]:  # Any: _started's arguments and row
        raise KeyboardInterrupt

    monkeypatch.setattr(suite, "_started", interrupted)
    task = suite.task_set("gallery")[0]
    with pytest.raises(KeyboardInterrupt):
        suite.one_run(task, 1, suite._Planner(), tmp_root=tmp_path)
    assert list(tmp_path.iterdir()) == []


class FakeProc:
    pid = 4242


def test_the_runs_own_chrome_is_stopped_and_the_slot_freed_on_an_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import steerai.probes.chrome_a11y as chrome  # noqa: PLC0415 - patched for this test

    proc = FakeProc()
    stopped: list[tuple[Any, Path]] = []  # Any: the fake process
    monkeypatch.setattr(suite, "_free_port", lambda: 9)
    launches: list[tuple[Any, ...]] = []  # Any: launch_chrome's arguments

    def launch(*args: Any, **kwargs: Any) -> FakeProc:  # Any: launch_chrome's arguments
        launches.append((args, kwargs))
        return proc

    monkeypatch.setattr(suite, "launch_chrome", launch)
    monkeypatch.setattr(chrome, "stop_chrome", lambda p, profile: stopped.append((p, profile)))

    def interrupted(*args: Any, **kwargs: Any) -> Any:  # Any: wait_window_for_pid's shape
        raise KeyboardInterrupt

    monkeypatch.setattr(suite, "wait_window_for_pid", interrupted)
    task = suite.task_set("gallery")[0]
    with pytest.raises(KeyboardInterrupt):
        suite._run_chrome(task, 1, tmp_path, suite._Planner(), "http://127.0.0.1:1/")
    assert stopped == [(proc, tmp_path / "profile")], "only the run's own Chrome and profile"
    with one_browser():
        pass  # the slot was freed


def test_a_second_browser_in_the_process_is_refused_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launched: list[object] = []
    monkeypatch.setattr(suite, "_free_port", lambda: 9)
    monkeypatch.setattr(suite, "launch_chrome", lambda *a, **k: launched.append((a, k)))
    claim_browser()
    try:
        with pytest.raises(BrowserBusyError):
            suite._run_chrome(
                suite.task_set("gallery")[0], 1, tmp_path, suite._Planner(), "http://x/"
            )
    finally:
        release_browser()
    assert launched == []
