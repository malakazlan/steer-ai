"""The runner's debug level (V2 step 1 packet v4 §A; invariants 4 and 7; hunter OB-07/13/41/43):
set only by a probe through `Options`, never by the command line or the environment; the sink
is opened beside the trace, handed to the session, and flushed once at the run's end. A flush
that fails is one closed token in the clear trace, and the run's outcome is the same."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

import steerai.run as run_mod
from steerai.debug_trace import BufferedDebugSink, NullDebugSink, debug_path_for
from steerai.run import Bound, UsageError, parse_args
from tests.orchestrator.test_loop import _type
from tests.orchestrator.test_run import (
    FakeSession,
    World,
    _done,
    _options,
    _plan,
    _run,
)

SENTINEL = "sentinel-Kp4 element name"


def _keyed(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


def _recording(world: World) -> None:
    """The session writes one debug record, as the loop would, through the bound sink."""
    deps_of = world.deps

    def deps() -> Any:
        deps = deps_of()
        build = deps.build_session

        def build_and_record(bound: Bound) -> Any:
            bound.debug.begin_step("s1")
            bound.debug.record("aim", {"name": SENTINEL})
            return build(bound)

        return deps.replace(build_session=build_and_record)

    world.deps = deps  # type: ignore[method-assign]  # a test seam


def test_the_level_is_never_set_by_the_command_line_or_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEER_TRACE_LEVEL", "debug")
    monkeypatch.setenv("STEER_DEBUG", "1")
    plan_path = tmp_path / "p.json"
    argv = ["--plan", str(plan_path), "--window", "chrome", "--ledger-dir", str(tmp_path)]
    assert parse_args(argv).trace_level == "redacted"
    with pytest.raises(UsageError):
        parse_args([*argv, "--trace-level", "debug"])


def test_debug_writes_one_protected_file_at_the_end_and_nothing_in_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_mod, "protect_for_user", _keyed)
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)))
    _recording(world)
    trace = tmp_path / "trace.jsonl"
    options = _options(tmp_path, plan, trace=trace, trace_level="debug")
    code, out, _err = _run(options, world)
    assert out["status"] == "done"
    assert isinstance(world.bound[0].debug, BufferedDebugSink)
    assert SENTINEL not in trace.read_text("utf-8")
    (line,) = debug_path_for(trace).read_bytes().splitlines()
    record = json.loads(_keyed(base64.b64decode(line)))
    assert (record["kind"], record["name"]) == ("aim", SENTINEL)
    plain_world = World(session=FakeSession(_done(plan)))
    (tmp_path / "p").mkdir()
    plain_code, plain_out, _ = _run(
        _options(tmp_path / "p", plan, trace=tmp_path / "t2.jsonl"), plain_world
    )
    assert (code, out["status"]) == (plain_code, plain_out["status"])


def test_a_protector_that_fails_is_one_token_and_the_same_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_data: bytes) -> bytes:
        raise OSError(f"no DPAPI for {SENTINEL}")

    monkeypatch.setattr(run_mod, "protect_for_user", broken)
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)))
    _recording(world)
    trace = tmp_path / "trace.jsonl"
    _code, out, err = _run(_options(tmp_path, plan, trace=trace, trace_level="debug"), world)
    assert out["status"] == "done"
    lines = [json.loads(x) for x in trace.read_text("utf-8").splitlines()]
    assert lines[-1] == {"kind": "debug", "detail": "unavailable"}
    assert SENTINEL not in trace.read_text("utf-8") + err
    assert not debug_path_for(trace).exists()


def test_no_trace_path_means_no_debug_file_anywhere(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)))
    _run(_options(tmp_path, plan, trace=None, trace_level="debug"), world)
    assert isinstance(world.bound[0].debug, NullDebugSink)
    assert not list(tmp_path.rglob("*.dpapi"))
