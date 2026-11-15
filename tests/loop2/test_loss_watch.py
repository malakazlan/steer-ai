"""[V2.64] Two, as ruled: real against slow monitor losses. A loss declared while a probe was
outstanding is watched after the task: the world hands its still-open monitor back as a
`LossWatch` instead of closing it; the CLI emits the final line first (flushed), then watches up
to `LOSS_WATCH_S` for the probe's echo ("slow"; none: "gone"; a kill or an unread fact ends it
with no verdict, which the harness reads as "unknown"), writes one `loss_watch` line, and closes
the monitor. Every other loss closes the monitor at once, as before."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, get_args

import pytest

from steerai.loop2 import cli, live_world, stage1
from steerai.loop2.live_world import LOSS_WATCH_S, LiveWorld, LossWatch
from steerai.loop2.loop import PortInterrupted, TaskOutcome
from steerai.loop2.stage1 import _read_out
from steerai.loop2.world_ports import KILL_POLL_S
from tests.loop2.test_cli_v2 import Run
from tests.loop2.test_live_world import Clock, Monitor, _world


def test_the_verdicts_are_the_harness_verdicts() -> None:
    assert get_args(live_world.LossVerdict) == get_args(stage1.LossVerdict)


def test_a_loss_with_a_probe_outstanding_hands_the_open_monitor_to_a_watch() -> None:
    world, platform = _world()
    platform.monitor.seen = False  # the probe is sent and never seen: the deadline loss
    with pytest.raises(PortInterrupted):
        world._prove_hooks()
    world.close()
    assert platform.monitor.closed == 0
    assert isinstance(world.loss_watch, LossWatch)
    assert "release_session" in platform.calls  # the rest of the teardown is unchanged


def test_a_loss_with_no_probe_outstanding_closes_the_monitor_as_before() -> None:
    world, platform = _world()
    platform.monitor.ok = False  # unhealthy, but every probe was seen
    assert world.interrupted() == "monitor_lost"
    world.close()
    assert platform.monitor.closed == 1
    assert world.loss_watch is None


def test_a_probe_read_that_fails_at_the_loss_is_not_watched() -> None:
    world, platform = _world()
    platform.monitor.ok = False

    def raises() -> bool:
        raise OSError("not read")

    platform.monitor.probe_seen = raises  # type: ignore[method-assign]  # a failing read
    assert world.interrupted() == "monitor_lost"
    world.close()
    assert (platform.monitor.closed, world.loss_watch) == (1, None)


def test_no_loss_closes_the_monitor_as_before() -> None:
    world, platform = _world()
    world.close()
    assert (platform.monitor.closed, world.loss_watch) == (1, None)


class _Echo(Monitor):
    def __init__(self, clock: Clock, echo_at: float | None) -> None:
        super().__init__()
        self.clock = clock
        self.echo_at = echo_at

    def probe_seen(self) -> bool:
        return self.echo_at is not None and self.clock() >= self.echo_at


def test_an_echo_within_the_watch_is_slow() -> None:
    clock = Clock()
    monitor = _Echo(clock, clock() + 0.4)
    assert LossWatch(monitor).run(clock, clock.pause) == ("slow", 400)
    assert monitor.closed == 1


def test_no_echo_within_the_watch_is_gone_and_the_watch_is_bounded() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)
    started = clock()
    assert LossWatch(monitor).run(clock, clock.pause) == ("gone", round(LOSS_WATCH_S * 1000))
    assert clock() - started == pytest.approx(LOSS_WATCH_S)
    assert monitor.closed == 1


def test_a_kill_ends_the_watch_with_no_verdict() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)
    monitor.killed = True
    assert LossWatch(monitor).run(clock, clock.pause) is None
    assert monitor.closed == 1


def test_a_read_that_fails_ends_the_watch_with_no_verdict() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)

    def raises() -> bool:
        raise OSError("not read")

    monitor.probe_seen = raises  # type: ignore[method-assign]  # a failing read
    assert LossWatch(monitor).run(clock, clock.pause) is None
    assert monitor.closed == 1


def test_the_watch_polls_no_faster_than_the_kill_poll() -> None:
    clock = Clock()
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        clock.pause(seconds)

    LossWatch(_Echo(clock, None)).run(clock, pause)
    assert pauses
    assert all(0 < p <= KILL_POLL_S for p in pauses)


def test_close_without_a_run_closes_the_monitor_once() -> None:
    monitor = Monitor()
    watch = LossWatch(monitor)
    watch.close()
    watch.close()
    assert monitor.closed == 1


# ---- the CLI: the final line first, then the loss_watch line ---------------------------------


class _Stdout(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushed = ""

    def flush(self) -> None:
        self.flushed = self.getvalue()


def _cli_with_watch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verdict: tuple[str, int] | None
) -> tuple[Run, list[str]]:
    run = Run(tmp_path)
    run.stdout = _Stdout()
    seen_at_run: list[str] = []

    class Watch:
        def run(self, _clock: Any, _pause: Any) -> tuple[str, int] | None:  # Any: clocks
            seen_at_run.append(run.stdout.flushed)  # type: ignore[attr-defined]  # _Stdout
            return verdict

        def close(self) -> None:
            seen_at_run.append("closed")

    real_close = LiveWorld.close

    def close(self: LiveWorld) -> None:
        real_close(self)
        self.loss_watch = Watch()  # type: ignore[assignment]  # a recording double

    monkeypatch.setattr(LiveWorld, "close", close)
    outcome = TaskOutcome(status="stopped", cause=None, dispatched=(), model_calls=1, strikes=())
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    return run, seen_at_run


@pytest.mark.parametrize("verdict", [("slow", 120), ("gone", 3000)])
def test_the_final_line_is_flushed_before_the_watch_and_the_verdict_follows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verdict: tuple[str, int]
) -> None:
    run, seen = _cli_with_watch(monkeypatch, tmp_path, verdict)
    code = run()
    lines = [json.loads(ln) for ln in run.stdout.getvalue().splitlines()]
    assert len(lines) == 2
    final, watch = lines
    assert final["status"] == "stopped"
    assert json.dumps(final, sort_keys=True) in seen[0]  # flushed before the watch began
    assert watch["loss_watch"] == {"verdict": verdict[0], "after_ms": verdict[1]}
    assert watch["task_id"] == final["task_id"]
    assert "status" not in watch
    assert code == cli.EXIT_FOR["stopped"]


def test_a_watch_with_no_verdict_writes_no_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run, _ = _cli_with_watch(monkeypatch, tmp_path, None)
    run()
    assert len(run.stdout.getvalue().splitlines()) == 1


def test_the_harness_reads_what_the_cli_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run, _ = _cli_with_watch(monkeypatch, tmp_path, ("slow", 120))
    run()
    out = tmp_path / "out.jsonl"
    out.write_text(run.stdout.getvalue(), encoding="utf-8")
    final, watch = _read_out(out)
    assert final is not None
    assert final["status"] == "stopped"
    assert watch == "slow"
