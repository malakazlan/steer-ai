"""[V2.14] 4: the suite in chunks, one child process at a time, with a pre-flight floor, a per-task
wall-time cap, and cleanup of only Steer's own child tree and temp folder on abort. Fakes only:
no process is started."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from steerai.probes.memory_guard import EXIT_LOW_MEMORY
from steerai.probes.suite_chunks import Unit, plan_chunks, run_chunked

BASE = ["--set", "gallery", "--runs", "1"]


def test_chunks_split_by_size_and_never_span_a_run_number() -> None:
    units = [Unit("g1", 1), Unit("g2", 1), Unit("g3", 1), Unit("g1", 2), Unit("g2", 2)]
    assert plan_chunks(units, 2) == [
        [Unit("g1", 1), Unit("g2", 1)],
        [Unit("g3", 1)],
        [Unit("g1", 2), Unit("g2", 2)],
    ]


def test_a_chunk_size_of_one_is_one_task_per_process() -> None:
    units = [Unit("g1", 1), Unit("g2", 1)]
    assert plan_chunks(units, 1) == [[Unit("g1", 1)], [Unit("g2", 1)]]


def test_a_chunk_size_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        plan_chunks([Unit("g1", 1)], 0)


class FakeChild:
    def __init__(self, world: World, argv: list[str]) -> None:
        self.world, self.argv, self.pid = world, argv, 999 + len(world.started)
        self.out = Path(argv[argv.index("--out") + 1])
        self.tasks = argv[argv.index("--tasks") + 1].split(",")
        self.number = int(argv[argv.index("--run-number") + 1])
        self.behaviour = world.behaviour.pop(0) if world.behaviour else "ok"

    def wait(self, timeout: float | None = None) -> int:
        if self.pid in self.world.killed:
            return 1  # reaped after the kill
        self.world.timeouts.append(timeout)
        behaviour = self.behaviour
        if behaviour == "hang":
            self._rows(self.tasks[:1])
            raise subprocess.TimeoutExpired(self.argv, timeout or 0.0)
        if behaviour == "interrupt":
            raise KeyboardInterrupt
        if behaviour == "low_memory":
            self._rows([])
            return EXIT_LOW_MEMORY
        self._rows(self.tasks)
        self.world.running -= 1
        return 0

    def _rows(self, tasks: list[str]) -> None:
        lines = [json.dumps({"header": True})]
        lines += [json.dumps({"task": t, "run": self.number, "passed": True}) for t in tasks]
        self.out.write_text("\n".join(lines) + "\n", encoding="utf-8")


class World:
    def __init__(self, behaviour: list[str] | None = None, free_mb: list[int] | None = None):
        self.behaviour = behaviour or []
        self.free = free_mb or []
        self.started: list[list[str]] = []
        self.killed: list[int] = []
        self.removed: list[Path] = []
        self.rows: list[dict[str, object]] = []
        self.timeouts: list[float | None] = []
        self.running = 0
        self.most_at_once = 0

    def start(self, argv: list[str]) -> FakeChild:
        self.started.append(argv)
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        return FakeChild(self, argv)

    def kill_tree(self, pid: int) -> None:
        self.killed.append(pid)
        self.running -= 1

    def remove(self, path: Path) -> None:
        self.removed.append(path)

    def read_free(self) -> int:
        return self.free.pop(0) if self.free else 8000

    def run(self, chunks: list[list[Unit]], tmp: Path, cap_s: float = 60.0) -> int:
        return run_chunked(
            chunks,
            base_args=BASE,
            cap_s=cap_s,
            floor_mb=4096,
            tmp_parent=tmp,
            write_row=self.rows.append,
            read_free=self.read_free,
            start=self.start,
            kill_tree=self.kill_tree,
            remove=self.remove,
        )


CHUNKS = [[Unit("g1", 1), Unit("g2", 1)], [Unit("g3", 1)]]


def test_every_chunk_runs_in_its_own_child_one_at_a_time(tmp_path: Path) -> None:
    world = World()
    assert world.run(CHUNKS, tmp_path) == 0
    assert len(world.started) == 2
    assert world.most_at_once == 1
    assert [r["task"] for r in world.rows] == ["g1", "g2", "g3"]
    assert world.killed == []


def test_the_child_gets_its_tasks_run_number_floor_and_own_temp_root(tmp_path: Path) -> None:
    world = World()
    world.run(CHUNKS, tmp_path)
    argv = world.started[0]
    assert argv[argv.index("--tasks") + 1] == "g1,g2"
    assert argv[argv.index("--run-number") + 1] == "1"
    assert argv[argv.index("--min-free-mb") + 1] == "4096"
    root = Path(argv[argv.index("--tmp-root") + 1])
    assert tmp_path in root.parents
    assert all(a in argv for a in BASE)


def test_the_wall_time_cap_is_per_task(tmp_path: Path) -> None:
    world = World()
    world.run(CHUNKS, tmp_path, cap_s=300.0)
    assert world.timeouts == [600.0, 300.0]


def test_every_chunk_folder_is_removed(tmp_path: Path) -> None:
    world = World()
    world.run(CHUNKS, tmp_path)
    assert len(world.removed) == 2
    assert all(tmp_path in p.parents for p in world.removed)


def test_below_the_floor_no_child_starts_and_the_run_exits_low_memory(tmp_path: Path) -> None:
    world = World(free_mb=[8000, 2000])
    assert world.run(CHUNKS, tmp_path) == EXIT_LOW_MEMORY
    assert len(world.started) == 1, "the first chunk ran; the second was refused before start"
    refusal = world.rows[-1]
    assert refusal["refused"] == "low_memory"
    assert (refusal["free_mb"], refusal["floor_mb"]) == (2000, 4096)
    assert refusal["remaining"] == ["g3:1"]


def test_a_child_that_refuses_low_memory_ends_the_run(tmp_path: Path) -> None:
    world = World(behaviour=["low_memory"])
    assert world.run(CHUNKS, tmp_path) == EXIT_LOW_MEMORY
    assert len(world.started) == 1


def test_a_task_over_its_cap_kills_only_that_childs_tree_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    world = World(behaviour=["hang"])
    assert world.run(CHUNKS, tmp_path) == 0
    assert world.killed == [1000], "the hung child's own tree, nothing else"
    tasks = [(r.get("task"), r.get("aborted")) for r in world.rows]
    assert tasks == [("g1", None), ("g2", "task_cap"), ("g3", None)]
    assert len(world.removed) == 2


def test_an_interrupt_kills_the_child_cleans_up_and_is_raised(tmp_path: Path) -> None:
    world = World(behaviour=["interrupt"])
    with pytest.raises(KeyboardInterrupt):
        world.run(CHUNKS, tmp_path)
    assert world.killed == [1000]
    assert len(world.removed) == 1
    assert [r.get("aborted") for r in world.rows] == ["interrupted", "interrupted"]
    assert len(world.started) == 1
