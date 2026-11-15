"""7b, [V2.53]: `--loop v2` switched on. The command line, the binding through v1's matcher, the
price file and budgets, the run header with every limit, the run on its loop thread with COM
released on every path, and the final JSON."""

from __future__ import annotations

import ast
import io
import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2 import cli
from steerai.loop2.cli import DEFAULT_BUDGETS, V2Deps, bind_window, parse_v2
from steerai.loop2.live_world import LiveWorld
from steerai.loop2.loop import Reason, TaskOutcome
from steerai.planner.claude_planner import DEFAULT_MODEL
from steerai.probes.windows import WindowInfo
from steerai.run import (
    EXIT_ERROR,
    EXIT_STOPPED,
    EXIT_USAGE,
    Deps,
    RunnerError,
    UsageError,
    _pick_window,
    main,
    parse_args,
)
from tests.action.fakes import TYPE_IDS
from tests.loop2.test_live_world import Clock, Platform, _Handle
from tests.loop2.world_fakes import PAGE

CONSOLE = WindowInfo(hwnd=7, title="Steer console", pid=70)
SHOP = WindowInfo(hwnd=16, title="Shop - Browser", pid=4242)
SHOP_TOO = WindowInfo(hwnd=17, title="Shop - Other", pid=4343)
SMALL = WindowInfo(hwnd=18, title="Mail", pid=5555, minimized=True)
WINDOWS = [CONSOLE, SHOP, SHOP_TOO, SMALL]
EXCLUDED = ({7}, {70})
TODAY = date(2026, 11, 15)
WHERE = ["--loop", "v2", "--goal", "search the shop for red shoes", "--hwnd", "16"]


@pytest.fixture(autouse=True)
def _localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#361 B1: the default price file lives under LOCALAPPDATA, which a Linux CI runner does
    not set; every test here runs with its own, and the H11 test removes it itself."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def _price_file(tmp_path: Path, **changes: object) -> Path:
    doc: dict[str, object] = {
        "model": DEFAULT_MODEL,
        "source": "https://docs.example.com/pricing",
        "read_on": "2026-11-10",
        "usd_per_million_tokens": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25,
                                   "cache_write_1h": 10.0, "cache_read": 0.5},
        "max_cost_usd_per_task": 0.5,
    }  # fmt: skip
    doc.update(changes)
    path = tmp_path / "price.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class Run:
    """One `--loop v2` run over fakes: the windows, the platform and the streams."""

    def __init__(self, tmp_path: Path, platform: Platform | None = None) -> None:
        self.platform = Platform() if platform is None else platform
        self.platform.handles = [  # each read stamped on the run's clock, as a live read is
            _Handle(control_type=TYPE_IDS.get(e.role, TYPE_IDS["Button"]), name=e.name)
            for e in PAGE
        ]
        self.price = _price_file(tmp_path)
        self.trace = tmp_path / "trace.jsonl"
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.ready = True
        self.image = "browser.exe"
        self.clock = Clock()
        self.deps = V2Deps(
            list_windows=lambda: WINDOWS,
            exclusions=lambda: EXCLUDED,
            platform=lambda: self.platform,
            today=lambda: TODAY,
            image_of=lambda _pid: self.image,
            ready=lambda _step: self.ready,
            clock=self.clock,
            pause=self.clock.pause,
        )

    def __call__(self, *extra: str, where: Sequence[str] = WHERE) -> int:
        argv = [*where, "--price-file", str(self.price), "--trace", str(self.trace), *extra]
        return cli.main(argv, deps=self.deps, stdout=self.stdout, stderr=self.stderr)

    def result(self) -> dict[str, Any]:  # Any: the final JSON
        lines = self.stdout.getvalue().splitlines()
        assert len(lines) == 1, "one JSON line on stdout"
        return json.loads(lines[0])  # type: ignore[no-any-return]  # a JSON object

    def header(self) -> dict[str, str]:
        lines = [ln for ln in self.trace.read_text(encoding="utf-8").splitlines() if ln]
        records = [json.loads(ln) for ln in lines]
        return next(r for r in records if r.get("event") == "run_header")


# ---- the command line ------------------------------------------------------------------------


def test_the_budgets_default_to_the_provisional_values() -> None:
    options = parse_v2([*WHERE])
    assert (
        options.max_model_calls,
        options.max_actions,
        options.max_tokens,
        options.max_wall_s,
    ) == (20, 40, 400_000, 300.0)
    assert DEFAULT_BUDGETS == (20, 40, 400_000, 300.0)


@pytest.mark.parametrize(
    "flag", ["--max-model-calls", "--max-actions", "--max-tokens", "--max-wall-s"]
)
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "x"])
def test_a_zero_negative_or_unbounded_budget_refuses(flag: str, value: str) -> None:
    with pytest.raises(UsageError):
        parse_v2([*WHERE, flag, value])


@pytest.mark.parametrize(
    "argv",
    [
        ["--loop", "v2", "--goal", "g"],  # no window
        ["--loop", "v2", "--hwnd", "16"],  # no goal
        ["--loop", "v2", "--goal", "g", "--hwnd", "16", "--window", "Shop"],
        ["--loop", "v2", "--plan", "p.json", "--hwnd", "16"],  # no --plan until step 7
        ["--loop", "v2", "--goal", "g", "--desktop"],  # no --desktop until step 7
        ["--loop", "v2", "--goal", "g", "--hwnd", "16", "--trace-level", "debug"],  # invariant 4
        ["--loop", "v2", "--goal", "g", "--hwnd", "16", "--max-act", "3"],  # no abbreviations
        ["--loop", "v1", "--goal", "g", "--hwnd", "16"],
        ["--loop", "v2", "--goal", " ", "--hwnd", "16"],
    ],
    ids=["no-window", "no-goal", "both", "plan", "desktop", "debug", "abbrev", "v1", "blank"],
)
def test_the_v2_command_line_takes_only_its_own_flags(argv: list[str]) -> None:
    with pytest.raises(UsageError):
        parse_v2(argv)


def test_a_usage_error_is_one_json_line_and_exit_1(tmp_path: Path) -> None:
    run = Run(tmp_path)
    assert run("--max-actions", "0") == EXIT_USAGE
    assert run.result()["detail"] == "usage"
    assert run.platform.calls == []  # nothing was built


def test_v2_refuses_unless_step_3_is_ready(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.ready = False
    assert run() == EXIT_USAGE
    assert run.result()["detail"] == "loop_v2_not_ready"
    assert run.platform.calls == []


def test_run_py_hands_loop_v2_to_the_v2_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def record(argv: Sequence[str], **_kwargs: object) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(cli, "main", record)
    assert main([*WHERE, "--max-actions", "3"]) == 0
    assert main(["--goal", "g", "--hwnd", "16", "--loop=v2"]) == 0
    assert seen == [[*WHERE, "--max-actions", "3"], ["--goal", "g", "--hwnd", "16", "--loop=v2"]]


def test_an_abbreviated_loop_flag_reaches_the_v2_parser_and_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--goal", "g", "--hwnd", "16", "--lo", "v2"]) == EXIT_USAGE
    assert '"detail": "usage"' in capsys.readouterr().out


# ---- the binding: v1's matcher, exactly one window, the console excluded ---------------------


def _v1_pick(hwnd: int | None, title: str | None) -> WindowInfo:
    where = ["--hwnd", str(hwnd)] if hwnd is not None else ["--window", str(title)]
    options = parse_args(["--goal", "g", *where])

    def never(*_a: object) -> Any:  # Any: v1's collaborators, never reached by the matcher
        raise AssertionError("the matcher touches nothing else")

    deps = Deps(
        list_windows=lambda: WINDOWS,
        exclusions=lambda: EXCLUDED,
        acquire_foreground=never,
        open_ledger=never,
        build_session=never,
    )
    return _pick_window(options, deps)


def _outcome_of(pick: Any) -> tuple[object, ...]:  # Any: either matcher
    try:
        window = pick()
    except RunnerError as exc:
        return ("refused", exc.code, exc.detail, tuple(w.hwnd for w in exc.candidates))
    except UsageError:
        return ("usage",)
    return ("bound", window.hwnd)


@pytest.mark.parametrize(
    ("hwnd", "title"),
    [
        (16, None),
        (7, None),
        (18, None),
        (99, None),
        (None, "Shop - Browser"),
        (None, "shop"),
        (None, "Mail"),
        (None, "Steer"),
        (None, "nothing"),
        (None, " "),
    ],
)
def test_the_binding_is_v1s_matcher(hwnd: int | None, title: str | None) -> None:
    ours = _outcome_of(lambda: bind_window(WINDOWS, EXCLUDED, hwnd=hwnd, title=title))
    assert ours == _outcome_of(lambda: _v1_pick(hwnd, title))


def test_no_match_refuses_and_lists_the_candidates(tmp_path: Path) -> None:
    run = Run(tmp_path)
    where = ["--loop", "v2", "--goal", "search the shop", "--window", "shop"]
    assert run(where=where) == EXIT_USAGE
    result = run.result()
    assert (result["code"], result["detail"]) == (
        ErrorCode.ELEMENT_NOT_FOUND.value,
        "ambiguous_target",
    )
    assert "hwnd 0x10" in run.stderr.getvalue()
    assert "hwnd 0x11" in run.stderr.getvalue()
    assert run.platform.calls == []


# ---- the price file and the header -------------------------------------------------------------


def test_a_bad_price_file_refuses_before_anything_is_built(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.price = _price_file(tmp_path, model="claude-other")
    assert run() == EXIT_USAGE
    assert (run.result()["code"], run.result()["detail"]) == ("gate_blocked", "price_model")
    assert run.platform.calls == []


def test_the_header_prints_every_limit_and_the_price(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run("--max-actions", "7")
    header = run.header()
    assert {k: header[k] for k in (
        "max_model_calls", "max_actions", "max_tokens", "max_wall_s", "max_cost_usd",
    )} == {"max_model_calls": "20", "max_actions": "7", "max_tokens": "400000",
           "max_wall_s": "300.0", "max_cost_usd": "0.5"}  # fmt: skip
    assert header["price"] == "configured"
    assert header["trace_level"] == "redacted"  # invariant 4: no flag reaches the debug level
    assert header["price_source"] == "https://docs.example.com/pricing"
    assert header["price_read_on"] == "2026-11-10"
    assert header["price_stale"] == "false"
    assert "header: " in run.stderr.getvalue()


def test_an_old_price_read_date_warns_in_the_header(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.price = _price_file(tmp_path, read_on="2026-01-01")
    run()
    assert run.header()["price_stale"] == "true"
    assert "warning: the price was read on 2026-01-01" in run.stderr.getvalue()


# ---- the run, its thread, and the final JSON -------------------------------------------------


def test_a_run_reports_use_against_every_limit_and_the_worlds_counts(tmp_path: Path) -> None:
    run = Run(tmp_path)
    code = run()
    result = run.result()
    assert result["status"] == "cannot"  # the fake model says it cannot
    assert code == cli.EXIT_FOR["cannot"]
    assert set(result["used"]) == {
        "max_model_calls", "max_actions", "max_tokens", "max_wall_s", "max_cost_usd",
    }  # fmt: skip
    assert result["used"]["max_model_calls"] == len(result["listings"]) >= 1  # one per turn
    assert result["used"]["max_actions"] == 0
    assert result["limits"]["max_actions"] == 40
    assert result["paused_by"] is None
    assert set(result["focus"]) == {
        "observations_focus_unread", "observations_focus_elsewhere", "type_unfocused",
        "focus_moved_after_press",
    }  # fmt: skip
    assert set(result["listings"][0]) == {
        "eligible", "shown", "cut", "keep_at_ceiling", "goal_at_subcap", "prompt_cut",
        "prompt_tokens",
    }  # fmt: skip
    assert result["world"] == {
        "trace_unwritable": 0,
        "late_sink_failures": 0,
        "debug_failure": None,
        "monitor_lost": 0,
        "hook_events": 0,
        "hook_latency_p95_ms": None,
        "hook_latency_p99_ms": None,
    }


def test_the_task_runs_on_its_loop_thread_and_com_is_released_once(tmp_path: Path) -> None:
    import threading  # noqa: PLC0415 - this test only

    run = Run(tmp_path)
    seen: list[str] = []
    original = run.platform.release_com

    def release() -> None:
        seen.append(threading.current_thread().name)
        original()

    run.platform.release_com = release  # type: ignore[method-assign]  # a recording fake
    run()
    assert seen == ["steer-v2-loop"]


def test_a_world_that_cannot_start_still_releases_com_and_says_why(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.platform.session = "held"
    assert run() == cli.EXIT_FOR["blocked"]
    result = run.result()
    assert (result["status"], result["code"], result["detail"]) == (
        "blocked",
        "gate_blocked",
        "steer_already_running",
    )
    assert run.platform.calls.count("release_com") == 1


def test_a_budget_that_paused_the_task_is_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paused = TaskOutcome(
        status="paused", cause=Reason("max_actions"), dispatched=(), model_calls=2, strikes=(),
        tokens=1200.0, cost_usd=0.01, wall_s=3.5,
    )  # fmt: skip
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: paused)
    run = Run(tmp_path)
    assert run() == cli.EXIT_FOR["paused"]
    result = run.result()
    assert result["paused_by"] == "max_actions"
    assert result["used"] == {
        "max_model_calls": 2, "max_actions": 0, "max_tokens": 1200.0, "max_wall_s": 3.5,
        "max_cost_usd": 0.01,
    }  # fmt: skip


def test_unknown_token_use_is_reported_as_incomplete_never_nan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = TaskOutcome(
        status="paused", cause=Reason("max_tokens"), dispatched=(), model_calls=1, strikes=(),
        tokens=float("nan"), cost_usd=float("nan"), wall_s=1.0,
    )  # fmt: skip
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: broken)
    run = Run(tmp_path)
    run()
    result = run.result()  # #361 P1: the line is written; a NaN cost would have lost it
    assert result["used"]["max_tokens"] is None
    assert result["used"]["max_cost_usd"] is None
    assert result["tokens_incomplete"] is True
    assert result["cost_incomplete"] is True
    assert "NaN" not in run.stdout.getvalue()


# ---- the act port is reachable only through LiveWorld -----------------------------------------


def test_the_act_port_is_built_only_by_live_world() -> None:
    package = Path(__file__).resolve().parents[2] / "steerai"
    builders = []
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ActPort":
                builders.append(path.name)
    assert builders == ["live_world.py"]


@pytest.mark.parametrize(
    ("argv", "asks"),
    [
        ([], False),
        (["--loop"], False),
        (["--loop", "v1"], False),
        (["--goal", "--loop", "--hwnd", "16"], False),
        (["--loop", "v2"], True),
        (["--goal", "g", "--loop=v2"], True),
    ],
)
def test_run_py_reads_the_loop_flag_before_v1s_parser(argv: list[str], asks: bool) -> None:
    from steerai.run import _asks_v2  # noqa: PLC0415 - this test only

    assert _asks_v2(argv) is asks


def test_an_unknown_wall_time_is_null_and_said(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#361 H8 (JSN-01): a clock that broke gives an infinite wall time; the line is still
    written, with the wall time null and flagged."""
    broken = TaskOutcome(
        status="paused", cause=Reason("max_wall"), dispatched=(), model_calls=1, strikes=(),
        tokens=10.0, cost_usd=0.0, wall_s=float("inf"),
    )  # fmt: skip
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: broken)
    run = Run(tmp_path)
    run()
    result = run.result()
    assert result["used"]["max_wall_s"] is None
    assert result["wall_incomplete"] is True
    assert result["paused_by"] == "max_wall"


def test_the_header_is_written_to_the_trace_once_with_every_limit(tmp_path: Path) -> None:
    """#361 H13 (HDR-01)."""
    run = Run(tmp_path)
    run()
    lines = run.trace.read_text(encoding="utf-8").splitlines()
    headers = [json.loads(ln) for ln in lines if '"run_header"' in ln]
    assert len(headers) == 1
    assert headers[0]["max_actions"] == "40"


def test_no_localappdata_and_no_price_file_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """#361 H11 (PRC-10): never a price file from the working directory."""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(UsageError, match="price"):
        parse_v2([*WHERE])
    assert parse_v2([*WHERE, "--price-file", "p.json"]).price_file == Path("p.json")


def test_the_cli_binds_only_the_process_it_listed(tmp_path: Path) -> None:
    """#361 H6: the listed window's pid and image go to the binding, which re-reads them."""
    run = Run(tmp_path)
    run.image = "other.exe"
    assert run() == cli.EXIT_FOR["blocked"]
    assert (run.result()["code"], run.result()["detail"]) == ("app_crashed", "window_changed")


def test_ctrl_c_still_writes_the_outcome_and_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#361 P2: every exit path writes exactly one JSON line; an interrupt during the join is
    said, the outcome kept, and the exit is EXIT_STOPPED."""
    from tests.loop2.test_live_world import _interrupt_the_first_join  # noqa: PLC0415

    _interrupt_the_first_join(monkeypatch)
    run = Run(tmp_path)
    assert run() == EXIT_STOPPED
    result = run.result()
    assert result["interrupted"] is True
    assert "used" in result
    assert "interrupted" in run.stderr.getvalue()
    assert run.platform.calls.count("release_com") == 1


def test_an_unexpected_error_is_internal_error_on_one_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#361 P2: as v1's `run()`: the traceback on stderr, escaped; one JSON line; exit 70."""

    def broken(_task: object, _ports: object) -> TaskOutcome:
        raise RuntimeError("a bug")

    monkeypatch.setattr(cli, "run_task", broken)
    run = Run(tmp_path)
    assert run() == EXIT_ERROR
    assert (run.result()["status"], run.result()["detail"]) == ("error", "internal_error")
    assert "RuntimeError" in run.stderr.getvalue()
    assert "in broken" in run.stderr.getvalue()  # #361 B2: the loop thread's own frames
    assert run.platform.calls.count("release_com") == 1


def test_a_steer_error_mid_task_reports_use_not_a_refused_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#361 H5 (CLI-04): the world started, so the line is the run's shape, with the use and
    the limits; the use that could not be counted is null and said."""

    def failed(_task: object, _ports: object) -> TaskOutcome:
        raise SteerError(ErrorCode.PERCEPTION_DEGRADED, "uia_timeout")

    monkeypatch.setattr(cli, "run_task", failed)
    run = Run(tmp_path)
    assert run() == EXIT_STOPPED
    result = run.result()
    assert (result["status"], result["reason"], result["error"]) == (
        "stopped", "port_perception_degraded", "uia_timeout",
    )  # fmt: skip
    assert result["limits"]["max_actions"] == 40
    assert result["used"]["max_model_calls"] is None
    assert result["tokens_incomplete"] is True
    assert result["listings"] == []
    assert result["cache_tokens"] == {"write_5m": None, "write_1h": None, "read": None}


def test_a_steer_error_after_the_task_keeps_its_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def report(_self: object) -> dict[str, object]:
        raise SteerError(ErrorCode.PERCEPTION_DEGRADED, "wrong_thread")

    monkeypatch.setattr(LiveWorld, "report", report)
    run = Run(tmp_path)
    assert run() == EXIT_STOPPED
    result = run.result()
    assert result["error"] == "wrong_thread"
    assert result["used"]["max_model_calls"] >= 1  # the task's own counts survive


def test_a_lost_monitor_is_reported_apart_from_steers_own_stops(tmp_path: Path) -> None:
    """[V2.59]: stage 1 counts runs ending monitor_lost on their own (more than 1 in 10
    rejects the stage), never mixed with Steer's stops."""
    run = Run(tmp_path)
    run.platform.monitor.ok = False
    run()
    lost = run.result()
    assert (lost["status"], lost["reason"], lost["monitor_lost"]) == (
        "stopped", "monitor_lost", True,
    )  # fmt: skip
    clean = Run(tmp_path)
    clean()
    assert clean.result()["monitor_lost"] is False


def test_the_final_line_carries_the_cache_counters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#366 P3, [V2.45]: stage 1 asserts every cache counter is zero, from this line."""
    from steerai.loop2.loop import TokenUsage  # noqa: PLC0415 - this test only

    usage = TokenUsage(input_tokens=10, output_tokens=5, cache_write_5m=1, cache_write_1h=2,
                       cache_read=3)  # fmt: skip
    outcome = TaskOutcome(
        status="done", cause=None, dispatched=(), model_calls=1, strikes=(), tokens=21.0,
        cost_usd=0.0, wall_s=1.0, usage=usage,
    )  # fmt: skip
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    run = Run(tmp_path)
    run()
    assert run.result()["cache_tokens"] == {"write_5m": 1, "write_1h": 2, "read": 3}
