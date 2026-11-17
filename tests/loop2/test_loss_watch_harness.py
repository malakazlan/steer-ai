"""[V2.64] Two, the coordinator's ruling on the loss watch: after a declared monitor loss the CLI
emits its final line first, then may write one `loss_watch` line ("slow" or "gone"). The stage
harnesses take the final line as the last line with a known CLI status, never just the last line,
and read the `loss_watch` line apart; a monitor_lost run with none is "unknown"."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from steerai.loop2 import stage1
from steerai.loop2.stage1 import (
    CHROME_LOST,
    NO_VERDICT_CAUSES,
    ChildRun,
    RunRecord,
    StageTask,
    accept,
    classify,
    run_child,
)
from tests.loop2.test_chrome_lost import _fake_live_edges, _lost_session
from tests.loop2.test_stage1 import DONE, _Child, _final, _ok, _record


def _ran(tmp_path: Path, lines: list[str]) -> tuple[object, object]:
    out = tmp_path / "out.jsonl"
    ran = run_child(["steer"], cap_s=60.0, out=out, start=lambda _a, _o: _Child(out, lines),
                    kill=lambda _pid: None)  # fmt: skip
    return ran.final, ran.loss_watch


def _watch(verdict: object) -> str:
    return json.dumps({"task_id": "t", "loss_watch": {"verdict": verdict, "after_ms": 120}})


def test_the_final_line_is_the_last_with_a_known_status_not_the_last_line(tmp_path: Path) -> None:
    final = _final()
    assert _ran(tmp_path, [json.dumps(final), _watch("slow")]) == (final, "slow")


def test_a_later_line_without_a_known_status_never_replaces_the_final_line(
    tmp_path: Path,
) -> None:
    final = _final()
    noise = [json.dumps({"status": "elsewhere"}), json.dumps({"probe": 1}), "{broken"]
    assert _ran(tmp_path, [json.dumps(final), *noise])[0] == final


def test_with_no_loss_watch_line_there_is_no_verdict(tmp_path: Path) -> None:
    assert _ran(tmp_path, [json.dumps(_final())])[1] is None


def test_a_loss_watch_verdict_off_the_closed_set_is_none(tmp_path: Path) -> None:
    for odd in ("late", 1, None, ["slow"]):
        assert _ran(tmp_path, [json.dumps(_final()), _watch(odd)])[1] is None


def test_the_verdict_is_carried_through_the_classification() -> None:
    lost = _final(monitor_lost=True)
    record = RunRecord("done", 1, lost, True, True, True, 0, loss_watch="gone")
    got = classify(DONE, record)
    assert (got.verdict, got.loss_watch) == ("monitor_lost", "gone")


def test_the_summary_tallies_real_slow_and_unknown_losses() -> None:
    runs = [
        _ok(DONE, 1, "monitor_lost", loss_watch="slow"),
        _ok(DONE, 2, "monitor_lost", loss_watch="gone"),
        _ok(DONE, 3, "monitor_lost"),
        _ok(DONE, 4, loss_watch="slow"),  # not a lost run: not counted
    ]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    json.dumps(report, allow_nan=False)
    assert report["monitor_losses"] == {"slow": 1, "gone": 1, "unknown": 1}


# ---- [V2.80]: a cap kill after the final line --------------------------------------------------


class _Late(_Child):
    """A child that writes its lines at once and then outlives the cap (the loss watch's 3 s)."""

    def __init__(self, out: Path, lines: list[str], *, cut: str = "") -> None:
        super().__init__(out, lines, hang=True)
        out.write_text("".join(f"{ln}\n" for ln in lines) + cut, encoding="utf-8")

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.waits == 1:
            raise subprocess.TimeoutExpired("steer", timeout or 0.0)
        return 0


def _capped(tmp_path: Path, lines: list[str], *, cut: str = "") -> ChildRun:
    out = tmp_path / "out.jsonl"
    return run_child(
        ["steer"], cap_s=1.0, out=out, start=lambda _a, _o: _Late(out, lines, cut=cut),
        kill=lambda _pid: None,
    )  # fmt: skip


def test_r8_a_complete_final_line_stands_when_the_cap_killed_the_child_after_it(
    tmp_path: Path,
) -> None:
    final = _final()
    ran = _capped(tmp_path, [json.dumps(final), _watch("slow")])
    assert (ran.final, ran.killed, ran.kill_after_final, ran.loss_watch) == (
        final,
        True,
        True,
        "slow",
    )


def test_r8_a_cut_off_final_line_counts_as_a_cap_kill(tmp_path: Path) -> None:
    cut = json.dumps(_final())[:40]  # the cap came mid-line
    ran = _capped(tmp_path, [], cut=cut)
    assert (ran.final, ran.killed, ran.kill_after_final) == (None, True, False)


def test_r8_a_cap_kill_with_no_final_line_is_a_cap_kill(tmp_path: Path) -> None:
    ran = _capped(tmp_path, [json.dumps({"probe": 1})])
    assert (ran.final, ran.killed, ran.kill_after_final) == (None, True, False)


def test_r8_a_final_line_before_the_kill_is_classified_on_its_own_outcome() -> None:
    record = _record(_final(), killed=True, kill_after_final=True)
    got = classify(DONE, record)
    assert (got.verdict, got.kill_after_final) == ("pass", True)


def test_r8_a_kill_that_did_not_end_the_child_still_fails_after_a_final_line() -> None:
    record = _record(_final(), killed=True, kill_after_final=True, kill_failure="child_survived")
    got = classify(DONE, record)
    assert (got.verdict, got.reason) == ("fail", "cap_killed:child_survived")


def test_r8_a_kill_with_no_final_line_still_reads_cap_killed() -> None:
    got = classify(DONE, RunRecord("a", 1, None, True, False, None, None, killed=True, code=None))
    assert (got.verdict, got.reason) == ("fail", "cap_killed")


def test_r8_the_summary_counts_the_kills_that_came_after_the_final_line() -> None:
    runs = [
        _ok(DONE, 1, kill_after_final=True),
        _ok(DONE, 2),
        _ok(DONE, 3, kill_after_final=True),
    ]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["cap_kills_after_final"] == 2


def test_r8_the_watch_seconds_are_not_wall_time() -> None:
    """max_wall_s is the final line's own (written before the watch); a verdict's `after_ms`
    never enters the wall quantiles."""
    runs = [_ok(DONE, r, "monitor_lost", loss_watch="gone") for r in (1, 2, 3)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["wall_s_p50"] == _final()["used"]["max_wall_s"]


# ---- [V2.80]: the no-verdict cause is kept ---------------------------------------------------


def _no_verdict(cause: object) -> str:
    return json.dumps({"task_id": "t", "loss_watch": {"no_verdict": cause}})


@pytest.mark.parametrize("cause", NO_VERDICT_CAUSES)
def test_a_no_verdict_cause_is_kept_not_folded_into_unknown(tmp_path: Path, cause: str) -> None:
    assert _ran(tmp_path, [json.dumps(_final()), _no_verdict(cause)])[1] == f"no_verdict:{cause}"


def test_a_no_verdict_cause_off_the_closed_set_is_none(tmp_path: Path) -> None:
    for odd in ("whatever", 1, None, ["killed"]):
        assert _ran(tmp_path, [json.dumps(_final()), _no_verdict(odd)])[1] is None


def test_the_summary_keeps_each_no_verdict_cause_apart() -> None:
    runs = [
        _ok(DONE, 1, "monitor_lost", loss_watch="no_verdict:not_deadline"),
        _ok(DONE, 2, "monitor_lost", loss_watch="no_verdict:not_deadline"),
        _ok(DONE, 3, "monitor_lost", loss_watch="no_verdict:killed"),
        _ok(DONE, 4, "monitor_lost", loss_watch="gone"),
        _ok(DONE, 5, "monitor_lost"),
    ]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=5, label="t").report
    assert report["loss_no_verdict"] == {"no_verdict:not_deadline": 2, "no_verdict:killed": 1}
    assert report["monitor_losses"] == {"slow": 0, "gone": 1, "unknown": 4}


def test_a_chrome_lost_record_carries_the_loss_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_live_edges(monkeypatch, _lost_session())

    class Child:
        pid = 5151

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - the Child shape
            return 0

    def start(_argv: list[str], out: Path) -> Child:
        lines = [json.dumps(_final("blocked", "controller_stop")), _watch("slow")]
        out.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")
        return Child()

    monkeypatch.setattr(stage1, "_start", start)
    task = StageTask("3d-a", "search the catalog for red shoes", "3d", "/a", "done")
    record = stage1._live_3d(task, 1, out_dir=tmp_path, price=tmp_path / "price.json")
    assert (record.lost, record.loss_watch) == (CHROME_LOST, "slow")
