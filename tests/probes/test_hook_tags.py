"""The tag-proof probe's pure parts over fake monitors: classification, the per-probe loop,
the summary and the guard. No hook, no input."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from steerai.probes import hook_tags
from steerai.probes.hook_tags import (
    HOOKS,
    LATE,
    LOST,
    PROVEN,
    Killed,
    format_table,
    hook_outcome,
    percentile,
    probe_outcome,
    run_case,
    summarise,
)

DEADLINE = 1.05
Row = dict[str, object]


class Fake:
    """A monitor and its reads on one fake clock; `script[i]` says, for probe i, each hook's echo
    delay in s (None: never), and `extra[i]` the (foreign, physical) counts that probe adds."""

    def __init__(
        self,
        script: list[dict[str, float | None]],
        extra: list[tuple[int, int]] | None = None,
        kill_at: int | None = None,
    ) -> None:
        self.t = 100.0
        self.script = script
        self.extra = extra or [(0, 0)] * len(script)
        self.kill_at = kill_at
        self.sent = -1
        self.sent_at = 0.0
        self.tags = dict.fromkeys(HOOKS, 0)
        self.at = dict.fromkeys(HOOKS, 0.0)
        self.counts = {"injected_other": 0, "user": 0}

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self._deliver()

    def _deliver(self) -> None:
        if self.sent < 0:
            return
        for hook in HOOKS:
            delay = self.script[self.sent][hook]
            if (
                delay is not None
                and self.t >= self.sent_at + delay
                and self.at[hook] < self.sent_at
            ):
                self.tags[hook] = self.sent + 1
                self.at[hook] = self.sent_at + delay

    def probe_now(self) -> None:
        self.sent += 1
        self.sent_at = self.t
        foreign, physical = self.extra[self.sent]
        self.counts["injected_other"] += foreign
        self.counts["user"] += physical

    def lost_hooks(self) -> tuple[str, ...]:
        late = self.sent_at + DEADLINE
        return tuple(h for h in HOOKS if not (self.tags[h] == self.sent + 1 and self.at[h] <= late))

    def probe_seen(self) -> bool:
        return not self.lost_hooks()

    def kill_requested(self) -> bool:
        return self.kill_at is not None and self.sent + 1 >= self.kill_at

    def proof(self, hook: str) -> int:
        return self.tags[hook]

    def proof_at(self, hook: str) -> float:
        return self.at[hook]

    def count(self, kind: str) -> int:
        return self.counts[kind]


def drive(fake: Fake, probes: int) -> list[hook_tags.Row]:
    seen: list[hook_tags.Row] = []
    rows = run_case(
        "hook-idle",
        fake,
        fake,
        probes=probes,
        interval_s=0.15,
        deadline_s=DEADLINE,
        sleep=fake.sleep,
        grace_s=0.5,
        warmup=0,
        emit=seen.append,
    )
    assert rows == seen
    return rows


def test_a_hook_is_proven_late_or_lost_by_its_echo_against_the_deadline() -> None:
    outcome, latency = hook_outcome(sent=10.0, proof_at=10.004, deadline_s=DEADLINE)
    assert outcome == PROVEN
    assert latency == pytest.approx(4.0)
    assert (
        hook_outcome(sent=10.0, proof_at=10.0 + DEADLINE - 0.001, deadline_s=DEADLINE)[0] == PROVEN
    )
    assert hook_outcome(sent=10.0, proof_at=11.2, deadline_s=DEADLINE)[0] == LATE
    assert hook_outcome(sent=10.0, proof_at=None, deadline_s=DEADLINE) == (LOST, None)


def test_a_probe_is_the_worst_of_its_hooks() -> None:
    assert probe_outcome({"keyboard": PROVEN, "mouse": PROVEN}) == PROVEN
    assert probe_outcome({"keyboard": LATE, "mouse": PROVEN}) == LATE
    assert probe_outcome({"keyboard": LATE, "mouse": LOST}) == LOST


def test_each_probe_records_latency_per_hook_and_outcomes() -> None:
    fake = Fake(
        [
            {"keyboard": 0.002, "mouse": 0.003},
            {"keyboard": 0.002, "mouse": None},
            {"keyboard": 1.2, "mouse": 0.004},
        ]
    )
    rows = drive(fake, 3)
    assert [r["outcome"] for r in rows] == [PROVEN, LOST, LATE]
    assert rows[0]["hooks"]["mouse"]["latency_ms"] == pytest.approx(3.0, abs=1.5)
    assert rows[1]["hooks"]["mouse"] == {"outcome": LOST, "latency_ms": None}
    assert rows[1]["monitor_lost"] == ["mouse"]
    assert rows[2]["hooks"]["keyboard"]["outcome"] == LATE
    assert rows[2]["monitor_lost"] == ["keyboard"], "the ledger counts a late echo as lost"
    assert rows[0]["monitor_lost"] == []


def test_probes_are_spaced_by_the_interval() -> None:
    fake = Fake([{"keyboard": 0.001, "mouse": 0.001}] * 3)
    rows = drive(fake, 3)
    gaps = [b["sent_s"] - a["sent_s"] for a, b in pairwise(rows)]
    assert gaps == [pytest.approx(0.15, abs=0.002)] * 2


def test_foreign_classed_probe_shaped_events_are_counted_per_probe() -> None:
    fake = Fake([{"keyboard": 0.001, "mouse": 0.001}] * 2, extra=[(0, 0), (2, 0)])
    rows = drive(fake, 2)
    assert [r["own_classed_foreign"] for r in rows] == [0, 2]
    assert summarise(rows)["own_classed_foreign"] == 2


def test_a_merged_move_is_an_unproven_mouse_with_physical_input_in_the_window() -> None:
    fake = Fake(
        [
            {"keyboard": 0.001, "mouse": None},
            {"keyboard": 0.001, "mouse": None},
            {"keyboard": None, "mouse": 0.001},
            {"keyboard": 0.001, "mouse": 0.001},
        ],
        extra=[(0, 5), (0, 0), (0, 3), (0, 9)],
    )
    rows = drive(fake, 4)
    assert [r["merged_move_suspect"] for r in rows] == [True, False, False, False]
    assert summarise(rows)["merged_move_suspects"] == 1


def test_the_kill_chord_stops_the_run_before_the_next_probe() -> None:
    fake = Fake([{"keyboard": 0.001, "mouse": 0.001}] * 5, kill_at=2)
    with pytest.raises(Killed):
        drive(fake, 5)
    assert fake.sent == 1


def test_warmup_probes_are_sent_but_not_recorded() -> None:
    fake = Fake([{"keyboard": 0.001, "mouse": 0.001}] * 4)
    rows = run_case(
        "hook-idle",
        fake,
        fake,
        probes=2,
        interval_s=0.15,
        deadline_s=DEADLINE,
        sleep=fake.sleep,
        warmup=2,
        emit=lambda _row: None,
    )
    assert fake.sent == 3
    assert [r["probe"] for r in rows] == [0, 1]


def test_percentiles_are_nearest_rank_and_empty_is_none() -> None:
    values = [float(i) for i in range(1, 101)]
    assert percentile(values, 0.5) == 50.0
    assert percentile(values, 0.95) == 95.0
    assert percentile([], 0.5) is None


def test_the_summary_counts_outcomes_and_reports_percentiles_per_hook() -> None:
    script: list[dict[str, float | None]] = [{"keyboard": 0.002, "mouse": 0.004}] * 8
    script += [{"keyboard": 0.002, "mouse": None}, {"keyboard": 1.3, "mouse": 0.004}]
    fake = Fake(script)
    summary = summarise(drive(fake, 10))
    assert summary["probes"] == 10
    assert summary["outcomes"] == {PROVEN: 8, LATE: 1, LOST: 1}
    assert summary["hooks"]["mouse"]["lost"] == 1
    assert summary["hooks"]["keyboard"]["late"] == 1
    assert summary["hooks"]["keyboard"]["max_ms"] == pytest.approx(1300.0, abs=2)
    assert summary["hooks"]["mouse"]["p50_ms"] == pytest.approx(4.0, abs=1.5)
    assert summary["monitor_disagrees"] == 0
    header = {
        "windows_build": "19045",
        "deadline_s": DEADLINE,
        "hook_timeout_ms": 1000,
        "interval_ms": 150.0,
    }
    table = format_table({"hook-idle": summary}, header)
    assert "19045" in table
    assert "hook-idle" in table
    assert "keyboard" in table
    assert "mouse" in table


def test_rows_hold_counts_and_times_only() -> None:
    fake = Fake([{"keyboard": 0.001, "mouse": 0.001}])
    text = json.dumps(drive(fake, 1))
    assert "vk" not in text
    assert "tag" not in text


def test_the_probe_refuses_to_run_without_i_am_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hook_tags.main(["--out", str(tmp_path)]) == 2
    assert "--i-am-present" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_the_prompts_tell_the_person_what_to_do() -> None:
    asked: list[str] = []

    def ask(text: str) -> str:
        asked.append(text)
        return ""

    hook_tags._prompt("hook-moving", 500, 0.15, ask)
    hook_tags._prompt("helper-idle", 500, 0.15, ask)
    assert "move the mouse continuously now for ~85 seconds" in asked[0]
    assert "press Enter to start" in asked[0]
    assert "do not touch" in asked[1]
