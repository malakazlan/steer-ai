"""[V2.64] Two, redesigned by [V2.80]: real against slow monitor losses.

Teardown is the default: only a world built with `watch_losses` (the CLI) hands its monitor to a
`LossWatch`, and only after a deadline loss in hook mode. The watch owns a thread anchored at the
loss instant that sends one watch probe after the loss and closes the monitor by
`loss_at + LOSS_WATCH_S` whatever any caller does. Every other loss writes `no_verdict:<cause>`.

The hunter's table ids were not available to this build; the tests carry the ruling's item numbers
(R1..R9, the "must have" list of the [V2.80] rebuild) instead."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any, get_args

import pytest

from steerai.loop2 import cli, live_world, stage1
from steerai.loop2.live_world import (
    LOSS_WATCH_S,
    LiveWorld,
    LossReport,
    LossWatch,
    NoVerdict,
)
from steerai.loop2.loop import PortInterrupted, TaskOutcome
from steerai.loop2.stage1 import _read_out
from steerai.loop2.world_ports import KILL_POLL_S
from tests.loop2.test_cli_v2 import Run
from tests.loop2.test_live_world import Clock, Monitor, Platform, _config

DEADLINE_S = 1.05
"""The probe deadline the watch is built with: the hook timeout cap plus the measurer margin."""


def test_the_verdicts_are_the_harness_verdicts() -> None:
    assert get_args(live_world.LossVerdict) == get_args(stage1.LossVerdict)


# ---- R1: teardown is the default; only a watching world hands off ------------------------------


def _lost_world(*, watch: bool) -> tuple[LiveWorld, Platform]:
    platform = Platform()
    platform.monitor.seen = False  # a probe sent and never seen: the deadline loss
    world = LiveWorld(_config(), platform, watch_losses=watch)
    with pytest.raises(PortInterrupted):
        world._prove_hooks()
    return world, platform


def test_r1_a_world_built_without_watch_losses_always_tears_down() -> None:
    world, platform = _lost_world(watch=False)
    world.close()
    assert platform.monitor.closed == 1
    assert (world.loss_watch, world.loss_report) == (None, None)
    assert "release_session" in platform.calls


def test_r1_a_watching_world_hands_a_deadline_loss_to_a_watch() -> None:
    world, platform = _lost_world(watch=True)
    world.close()
    assert isinstance(world.loss_watch, LossWatch)
    assert "release_session" in platform.calls  # the session mutex goes at close, on this thread
    assert world.loss_watch.run().verdict == "gone"  # the hook never answered the watch probe
    assert platform.monitor.closed == 1


def test_r1_a_loss_that_is_not_a_deadline_loss_is_torn_down_with_its_reason() -> None:
    platform = Platform()
    world = LiveWorld(_config(), platform, watch_losses=True)
    platform.monitor.ok = False  # unhealthy, but no probe missed its deadline
    assert world.interrupted() == "monitor_lost"
    world.close()
    assert (platform.monitor.closed, world.loss_watch) == (1, None)
    assert world.loss_report == LossReport(None, None, "not_deadline")


def test_r1_no_loss_closes_the_monitor_and_writes_no_record() -> None:
    platform = Platform()
    world = LiveWorld(_config(), platform, watch_losses=True)
    world.close()
    assert (platform.monitor.closed, world.loss_watch, world.loss_report) == (1, None, None)


def test_r4_helper_mode_is_never_watched() -> None:
    class Helper(Monitor):
        def session_change(self) -> str | None:
            return None

        def wait(self, _seconds: float) -> None:
            return None

    platform = Platform()
    helper = Helper()
    helper.seen = False
    platform.monitor = helper
    world = LiveWorld(_config(), platform, watch_losses=True)
    world._helper_monitor = helper  # helper mode, as the world sets it
    with pytest.raises(PortInterrupted):
        world._prove_hooks()
    world.close()
    assert (helper.closed, world.loss_watch) == (1, None)
    assert world.loss_report == LossReport(None, None, "helper_mode")


def test_r9_a_lost_hooks_read_that_fails_is_traced_with_a_code_and_has_no_verdict() -> None:
    platform = Platform()
    platform.monitor.seen = False

    def raises() -> tuple[str, ...]:
        raise OSError("not read")

    platform.monitor.lost_hooks = raises  # type: ignore[method-assign]  # a failing read
    config = _config()
    world = LiveWorld(config, platform, watch_losses=True)
    with pytest.raises(PortInterrupted):
        world._prove_hooks()
    world.close()
    assert world.loss_report == LossReport(None, None, "read_failed")
    assert platform.monitor.closed == 1
    lines = config.trace.lines  # type: ignore[attr-defined]  # a ListTrace
    assert any('"code": "perception_degraded"' in ln for ln in lines)


def test_the_loss_instant_is_stamped_once_on_the_worlds_clock() -> None:
    world, _ = _lost_world(watch=True)
    first = world._loss_at
    assert first is not None
    world._lose()
    assert world._loss_at == first
    world.close()
    assert world.loss_watch is not None
    world.loss_watch.run()


# ---- the watch ---------------------------------------------------------------------------------


class _Echo(Monitor):
    """Sees the watch probe `echo_after` s after it was sent; never when None."""

    def __init__(self, clock: Clock, echo_after: float | None) -> None:
        super().__init__()
        self.clock = clock
        self.echo_after = echo_after
        self.sent_at: float | None = None
        self.order: list[str] = []

    def probe_now(self) -> None:
        super().probe_now()
        self.order.append("probe_now")
        self.sent_at = self.clock()

    def probe_seen(self) -> bool:
        self.order.append("probe_seen")
        return (
            self.echo_after is not None
            and self.sent_at is not None
            and self.clock() >= self.sent_at + self.echo_after
        )

    def maintain(self) -> None:
        self.order.append("maintain")

    def healthy(self) -> bool:
        self.order.append("healthy")
        return True


def _watch(
    monitor: Monitor,
    clock: Clock,
    *,
    since_loss: float = 0.0,
    pause: Any = None,  # Any: a test's pause callable
    trace: Any = None,  # Any: a test's trace sink
) -> LossWatch:
    return LossWatch(
        monitor,
        loss_at=clock() - since_loss,
        probe_deadline_s=DEADLINE_S,
        clock=clock,
        pause=clock.pause if pause is None else pause,
        trace=trace,
    )


def test_r3_a_watch_probe_seen_inside_its_deadline_is_slow() -> None:
    clock = Clock()
    monitor = _Echo(clock, 0.4)
    assert _watch(monitor, clock).start().run() == LossReport("slow", 400, None)
    assert monitor.closed == 1


def test_r3_a_watch_probe_never_seen_is_gone_after_its_own_deadline() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)
    report = _watch(monitor, clock).start().run()
    assert (report.verdict, report.no_verdict) == ("gone", None)
    assert report.after_ms == pytest.approx(DEADLINE_S * 1000, abs=KILL_POLL_S * 1000)
    assert monitor.closed == 1


def test_r3_the_watch_sends_its_probe_before_it_reads_any_verdict() -> None:
    clock = Clock()
    monitor = _Echo(clock, 0.2)
    _watch(monitor, clock).start().run()
    assert monitor.order[0] == "probe_now"  # a verdict is the answer to a probe after the loss


def test_r5_the_watch_sends_nothing_but_its_one_probe() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)
    _watch(monitor, clock).start().run()
    assert monitor.probes == 1
    assert "maintain" not in monitor.order
    assert monitor.maintained == 0


def test_r3_the_watch_polls_no_faster_than_the_kill_poll() -> None:
    clock = Clock()
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        clock.pause(seconds)

    _watch(_Echo(clock, None), clock, pause=pause).start().run()
    assert pauses
    assert all(0 < p <= KILL_POLL_S for p in pauses)


def test_r2_the_monitor_is_closed_inside_the_window_whatever_the_caller_does() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)
    loss_at = clock() - 1.5
    watch = LossWatch(
        monitor,
        loss_at=loss_at,
        probe_deadline_s=DEADLINE_S,
        clock=clock,
        pause=clock.pause,
    ).start()
    watch._thread.join(timeout=5)  # nobody calls `run` or `close`
    assert monitor.closed == 1
    assert clock() - loss_at <= LOSS_WATCH_S + KILL_POLL_S


def test_r2_a_handoff_after_the_window_closes_at_once_with_no_verdict() -> None:
    clock = Clock()
    monitor = _Echo(clock, 0.1)
    report = _watch(monitor, clock, since_loss=LOSS_WATCH_S + 0.5).start().run()
    assert report == LossReport(None, None, "late_handoff")
    assert (monitor.probes, monitor.closed) == (0, 1)


def test_r3_a_window_too_short_for_the_probes_deadline_has_no_verdict() -> None:
    clock = Clock()
    monitor = _Echo(clock, 0.1)
    report = _watch(monitor, clock, since_loss=LOSS_WATCH_S - 0.5).start().run()
    assert report == LossReport(None, None, "no_probe")
    assert (monitor.probes, monitor.closed) == (0, 1)


def test_r5_a_kill_before_the_probe_ends_the_watch_at_once_with_no_verdict() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)
    monitor.killed = True
    assert _watch(monitor, clock).start().run() == LossReport(None, None, "killed")
    assert (monitor.probes, monitor.closed) == (0, 1)


def test_r5_a_kill_during_the_watch_ends_it_with_no_verdict() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)

    def pause(seconds: float) -> None:
        clock.pause(seconds)
        monitor.killed = True

    report = _watch(monitor, clock, pause=pause).start().run()
    assert report == LossReport(None, None, "killed")
    assert monitor.closed == 1


def test_r2_a_clock_that_fails_is_no_verdict_and_the_monitor_still_closes() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)
    traced: list[Any] = []

    def broken() -> float:
        raise OSError("clock")

    watch = LossWatch(
        monitor,
        loss_at=clock(),
        probe_deadline_s=DEADLINE_S,
        clock=broken,
        pause=clock.pause,
        trace=traced.append,
    )
    assert watch.start().run() == LossReport(None, None, "read_failed")  # `started` read inside
    assert monitor.closed == 1
    assert traced[0]["code"] == "perception_degraded"  # r9: the swallow leaves a coded event


def test_r2_the_watch_is_bounded_by_iterations_as_well_as_by_the_clock() -> None:
    clock = Clock()
    monitor = _Echo(clock, None)
    report = _watch(monitor, clock, pause=lambda _s: None).start().run()  # time never moves
    assert report == LossReport(None, None, "overrun")
    assert monitor.closed == 1


def test_r2_run_only_joins_and_a_watch_that_outruns_the_join_is_closed_by_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_world, "LOSS_WATCH_JOIN_S", 0.05)
    clock = Clock()
    release = threading.Event()

    class Stuck(_Echo):
        def probe_now(self) -> None:
            release.wait(timeout=5)

    monitor = Stuck(clock, None)
    watch = _watch(monitor, clock).start()
    try:
        assert watch.run() == LossReport(None, None, "overrun")
        assert monitor.closed == 1
    finally:
        release.set()
        watch._thread.join(timeout=5)
    assert monitor.closed == 1  # the late thread's own close found it closed already


def test_r2_close_is_idempotent_across_threads() -> None:
    clock = Clock()
    monitor = Monitor()
    watch = _watch(monitor, clock)
    threads = [threading.Thread(target=watch.close) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    watch.close()
    assert monitor.closed == 1


def test_r2_closed_is_marked_only_after_the_monitor_close_returns() -> None:
    clock = Clock()
    seen: list[bool] = []
    holder: list[LossWatch] = []

    class Slow(Monitor):
        def close(self) -> None:
            seen.append(holder[0]._closed)  # still False while the close runs

    holder.append(_watch(Slow(), clock))
    holder[0].close()
    assert seen == [False]
    assert holder[0]._closed is True


class _Flaky(_Echo):
    def __init__(self, clock: Clock, failures: int) -> None:
        super().__init__(clock, None)
        self.failures = failures

    def close(self) -> None:
        if self.failures:
            self.failures -= 1
            raise OSError("close")
        super().close()


def test_r2_a_close_that_fails_once_is_tried_again_and_traced() -> None:
    clock = Clock()
    traced: list[Any] = []
    once = _Flaky(clock, 1)
    _watch(once, clock, trace=traced.append).start().run()
    assert once.closed == 1  # the thread's second try closed it
    assert traced[0]["code"] == "gate_blocked"


def test_r2_a_close_that_keeps_failing_is_never_marked_closed_and_the_backstop_retries() -> None:
    clock = Clock()
    stuck = _Flaky(clock, 2)
    watch = _watch(stuck, clock).start()
    watch.run()
    assert (stuck.closed, watch._closed) == (0, False)  # left unclosed, never marked
    watch.close()  # the CLI's backstop
    assert stuck.closed == 1


# ---- the verdict records and the harness ---------------------------------------------------------


@pytest.mark.parametrize("cause", get_args(NoVerdict))
def test_every_no_verdict_cause_is_a_closed_token_the_harness_reads_as_unknown(
    cause: str, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    line = {"task_id": "t", "loss_watch": {"no_verdict": cause}}
    out.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert _read_out(out) == (None, None)


# ---- the CLI: the final line first, then the loss_watch record -----------------------------------


class _Stdout(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushed = ""
        self.fail_after: int | None = None

    def write(self, text: str, /) -> int:
        if self.fail_after is not None and self.getvalue().count("\n") >= self.fail_after:
            raise OSError("stdout closed")
        return super().write(text)

    def flush(self) -> None:
        self.flushed = self.getvalue()


class _Watch:
    def __init__(self, run: Any) -> None:  # Any: a test's callable
        self._run = run
        self.closed = 0

    def run(self) -> LossReport:
        report: LossReport = self._run()
        return report

    def close(self) -> None:
        self.closed += 1


def _cli_with_watch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_watch: Any,  # Any: a test's callable
) -> tuple[Run, list[str], _Watch]:
    run = Run(tmp_path)
    stdout = _Stdout()
    run.stdout = stdout
    seen_at_run: list[str] = []

    def at_run() -> LossReport:
        seen_at_run.append(stdout.flushed)
        report: LossReport = run_watch()
        return report

    watch = _Watch(at_run)
    real_close = LiveWorld.close

    def close(self: LiveWorld) -> None:
        real_close(self)
        self.loss_watch = watch  # type: ignore[assignment]  # a recording double

    monkeypatch.setattr(LiveWorld, "close", close)
    outcome = TaskOutcome(status="stopped", cause=None, dispatched=(), model_calls=1, strikes=())
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    return run, seen_at_run, watch


@pytest.mark.parametrize(
    ("report", "record"),
    [
        (LossReport("slow", 120, None), {"verdict": "slow", "after_ms": 120}),
        (LossReport("gone", 1050, None), {"verdict": "gone", "after_ms": 1050}),
        (LossReport(None, None, "killed"), {"no_verdict": "killed"}),
    ],
)
def test_the_final_line_is_flushed_before_the_watch_and_its_record_follows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, report: LossReport, record: dict[str, Any]
) -> None:
    run, seen, watch = _cli_with_watch(monkeypatch, tmp_path, lambda: report)
    code = run()
    lines = [json.loads(ln) for ln in run.stdout.getvalue().splitlines()]
    assert len(lines) == 2
    final, loss = lines
    assert final["status"] == "stopped"
    assert json.dumps(final, sort_keys=True) in seen[0]  # flushed before the watch began
    assert loss["loss_watch"] == record
    assert loss["task_id"] == final["task_id"]
    assert "status" not in loss  # a record type of its own
    assert code == cli.EXIT_FOR["stopped"]
    assert watch.closed == 1


def test_r9_a_ctrl_c_during_the_watch_never_replaces_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def interrupted() -> LossReport:
        raise KeyboardInterrupt

    run, _, watch = _cli_with_watch(monkeypatch, tmp_path, interrupted)
    assert run() == cli.EXIT_FOR["stopped"]
    assert watch.closed == 1  # the monitor is closed now, not left to the thread
    assert len(run.stdout.getvalue().splitlines()) == 1


def test_r9_a_record_that_cannot_be_written_never_replaces_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run, _, watch = _cli_with_watch(monkeypatch, tmp_path, lambda: LossReport("slow", 5, None))
    stdout = run.stdout
    assert isinstance(stdout, _Stdout)
    stdout.fail_after = 1  # the final line goes out; the record's write fails
    assert run() == cli.EXIT_FOR["stopped"]
    assert watch.closed == 1


def test_r9_a_close_that_fails_after_the_watch_never_replaces_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run, _, watch = _cli_with_watch(monkeypatch, tmp_path, lambda: LossReport("gone", 1050, None))

    def bad_close() -> None:
        raise OSError("close")

    watch.close = bad_close  # type: ignore[method-assign]  # a failing backstop
    assert run() == cli.EXIT_FOR["stopped"]


def test_an_unwatched_loss_writes_its_no_verdict_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = Run(tmp_path)
    real_close = LiveWorld.close

    def close(self: LiveWorld) -> None:
        real_close(self)
        self.loss_report = LossReport(None, None, "not_deadline")

    monkeypatch.setattr(LiveWorld, "close", close)
    outcome = TaskOutcome(status="stopped", cause=None, dispatched=(), model_calls=1, strikes=())
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    run()
    _, loss = (json.loads(ln) for ln in run.stdout.getvalue().splitlines())
    assert loss["loss_watch"] == {"no_verdict": "not_deadline"}


def test_r1_only_the_cli_builds_a_world_that_watches_losses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kinds: list[bool] = []
    real_init = LiveWorld.__init__

    def init(
        self: LiveWorld,
        config: Any,  # Any: the world's config, passed on
        platform: Any,  # Any: the platform, passed on
        *,
        watch_losses: bool = False,
    ) -> None:
        kinds.append(watch_losses)
        real_init(self, config, platform, watch_losses=watch_losses)

    monkeypatch.setattr(LiveWorld, "__init__", init)
    outcome = TaskOutcome(status="stopped", cause=None, dispatched=(), model_calls=1, strikes=())
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    Run(tmp_path)()
    assert kinds == [True]


def test_the_harness_reads_what_the_cli_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run, _, _ = _cli_with_watch(monkeypatch, tmp_path, lambda: LossReport("slow", 120, None))
    run()
    out = tmp_path / "out.jsonl"
    out.write_text(run.stdout.getvalue(), encoding="utf-8")
    final, watch = _read_out(out)
    assert final is not None
    assert final["status"] == "stopped"
    assert watch == "slow"
