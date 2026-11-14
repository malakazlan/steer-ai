"""[V2.19] item 3: the live checks run as one resumable command with one report."""

from __future__ import annotations

import json
from pathlib import Path

from steerai.probes.live_checks import Check, child_env, plan_lines, run_checks
from steerai.probes.memory_guard import EXIT_LOW_MEMORY

A = Check("a", "the floor", 5, ("mod.a", "--out", "{dir}/a.jsonl"))
GAP = Check("gap", "a later rule", 10, None)
B = Check("b", "the table", 7, ("mod.b",))
CHECKS = (A, GAP, B)


class Runs:
    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.codes = codes or {}

    def __call__(self, argv: list[str], stem: Path, env: dict[str, str]) -> int:
        self.argvs.append(argv)
        self.envs.append(env)
        return self.codes.get(stem.name, 0)


def _report(folder: Path) -> list[dict[str, object]]:
    lines = (folder / "report.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_the_plan_names_each_check_its_state_estimate_and_memory_need() -> None:
    lines = plan_lines(CHECKS, {"a": 0}, 4096)
    assert lines[0].startswith(" 1. a: done; unblocks the floor; ~5 min (estimate)")
    assert "gap: not built" in lines[1]
    assert lines[2].endswith("needs >= 4096 MB free")


def test_built_checks_run_in_order_and_a_gap_is_reported_not_skipped_silently(
    tmp_path: Path,
) -> None:
    runs = Runs()
    code = run_checks(tmp_path, checks=CHECKS, run=runs, read_free=lambda: 9000, write=str)
    assert code == 0
    assert [argv[2] for argv in runs.argvs] == ["mod.a", "mod.b"]
    assert runs.argvs[0][-1] == f"{tmp_path}/a.jsonl", "{dir} is the report folder"
    assert [r["status"] for r in _report(tmp_path)] == ["finished", "not_built", "finished"]


def test_a_second_run_skips_what_finished(tmp_path: Path) -> None:
    run_checks(tmp_path, checks=CHECKS, run=Runs(), read_free=lambda: 9000, write=str)
    again = Runs()
    run_checks(tmp_path, checks=CHECKS, run=again, read_free=lambda: 9000, write=str)
    assert again.argvs == []


def test_low_memory_stops_before_a_check_and_the_next_run_resumes_there(tmp_path: Path) -> None:
    free = iter([9000, 100])
    runs = Runs()
    code = run_checks(tmp_path, checks=CHECKS, run=runs, read_free=lambda: next(free), write=str)
    assert code == EXIT_LOW_MEMORY
    assert [argv[2] for argv in runs.argvs] == ["mod.a"]
    resumed = Runs()
    run_checks(tmp_path, checks=CHECKS, run=resumed, read_free=lambda: 9000, write=str)
    assert [argv[2] for argv in resumed.argvs] == ["mod.b"]


def test_a_check_that_refuses_for_memory_is_not_marked_done(tmp_path: Path) -> None:
    code = run_checks(
        tmp_path,
        checks=CHECKS,
        run=Runs({"a": EXIT_LOW_MEMORY}),
        read_free=lambda: 9000,
        write=str,
    )
    assert code == EXIT_LOW_MEMORY
    again = Runs()
    run_checks(tmp_path, checks=CHECKS, run=again, read_free=lambda: 9000, write=str)
    assert [argv[2] for argv in again.argvs] == ["mod.a", "mod.b"]


def test_the_key_comes_from_the_env_file_only_when_unset(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('anthropic-api-key = "sk-test-value"\n', encoding="utf-8")
    assert child_env({}, env_file)["ANTHROPIC_API_KEY"] == "sk-test-value"
    assert child_env({"ANTHROPIC_API_KEY": "set"}, env_file)["ANTHROPIC_API_KEY"] == "set"
    assert "VIRTUAL_ENV" not in child_env({"VIRTUAL_ENV": "x"}, env_file)
    assert "ANTHROPIC_API_KEY" not in child_env({}, tmp_path / "missing")


def test_the_plan_never_prints_the_key(tmp_path: Path) -> None:
    printed: list[str] = []
    env = {"ANTHROPIC_API_KEY": "sk-test-secret"}
    run_checks(
        tmp_path, checks=CHECKS, run=Runs(), read_free=lambda: 9000, env=env, write=printed.append
    )
    assert "sk-test-secret" not in "".join(printed)
    assert "sk-test-secret" not in (tmp_path / "report.jsonl").read_text("utf-8")
