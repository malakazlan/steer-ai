"""[V2.64] Two, the coordinator's ruling on the loss watch: after a declared monitor loss the CLI
emits its final line first, then may write one `loss_watch` line ("slow" or "gone"). The stage
harnesses take the final line as the last line with a known CLI status, never just the last line,
and read the `loss_watch` line apart; a monitor_lost run with none is "unknown"."""

from __future__ import annotations

import json
from pathlib import Path

from steerai.loop2.stage1 import RunRecord, accept, classify, run_child
from tests.loop2.test_stage1 import DONE, _Child, _final, _ok


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
