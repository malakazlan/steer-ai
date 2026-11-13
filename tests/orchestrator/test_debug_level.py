"""V2 step 1 packet v4, invariants 1, 2 and 8 at the debug level: with the debug channel on,
v1 is the golden fixture, byte for byte (results, task result, clear trace lines, fake call logs
and the fake clock, so no wait moved). And what the channel holds (the address bar's URLs, the
expect, titles) is in the protected file, never in the clear trace."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

import steerai.orchestrator.loop as loop_mod
from steerai.debug_trace import BufferedDebugSink
from tests.orchestrator import test_url_query_loop as url
from tests.orchestrator import test_v1_golden as golden


def _keyed(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


@pytest.fixture
def sinks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[BufferedDebugSink]:
    """Every Session built in the test gets a buffered debug sink instead of the null one."""
    made: list[BufferedDebugSink] = []

    def buffered() -> BufferedDebugSink:
        sink = BufferedDebugSink(tmp_path / f"d{len(made)}.dpapi", protect=_keyed)
        made.append(sink)
        return sink

    monkeypatch.setattr(loop_mod, "NullDebugSink", buffered)
    return made


def _decoded(sink: BufferedDebugSink, path: Path) -> list[dict[str, Any]]:
    assert sink.flush() is None
    return [
        json.loads(_keyed(base64.b64decode(line)).decode("utf-8"))
        for line in path.read_bytes().splitlines()
    ]


@pytest.mark.parametrize("name", sorted(golden.SCENARIOS))
def test_v1_is_the_golden_with_the_debug_channel_on(
    name: str, sinks: list[BufferedDebugSink]
) -> None:
    fixture = json.loads(golden.FIXTURE.read_text("utf-8"))
    assert golden._strip(fixture[name]) == golden.SCENARIOS[name](), name
    assert sinks, "the scenario ran with the channel on"


def test_the_urls_go_to_the_protected_file_never_the_clear_trace(
    sinks: list[BufferedDebugSink], tmp_path: Path
) -> None:
    bar = url.Bar(url.HOME, url.RESULTS)
    h = url._harness(bar, loaded="Bing")
    result = h.session.run_step(url._submit())
    clear = "\n".join(h.trace.lines)
    assert "bing.com" not in clear
    assert "bing.com" not in json.dumps(result.to_dict())
    sink = sinks[-1]  # the harness rebuilds its Session: the last one ran the step
    records = _decoded(sink, tmp_path / f"d{len(sinks) - 1}.dpapi")
    proof = next(r for r in records if r["kind"] == "url_proof")
    assert proof["leg"] == "no_load"
    assert proof["before_url"] == url.HOME
    assert url.RESULTS in proof["reads"]
    verify = next(r for r in records if r["kind"] == "verify")
    assert verify["expect"]["type"] == "Appeared"
    assert {r["step_id"] for r in records} == {"s3"}
