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
    assert any(sink._pending for sink in sinks), "the channel was on and built records"


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


def test_credentials_reach_neither_file_and_a_key_shaped_name_only_the_protected_one(
    sinks: list[BufferedDebugSink], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 3 (hunter OB-03): the debug channel reads screens, never the environment; a
    key-shaped string on screen is page data, so it is protected, never in clear."""
    secret = "sk-ant-api03-SENTINEL0000000000"  # noqa: S105 - a fake key, the sentinel
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, secret)
    shown = "sk-ant-SHOWNONPAGE000000"
    bar = url.Bar(url.HOME, url.RESULTS)
    h = url._harness(bar, loaded=shown)
    h.session.run_step(url._submit())
    clear = "\n".join(h.trace.lines)
    sink = sinks[-1]
    path = tmp_path / f"d{len(sinks) - 1}.dpapi"
    decoded = json.dumps(_decoded(sink, path))
    assert secret not in clear
    assert secret not in decoded
    assert secret.encode() not in path.read_bytes()
    assert shown not in clear
    assert shown in decoded, "the page's own text is what the debug file is for"


def test_the_document_url_is_recorded_when_the_load_check_read_it(
    sinks: list[BufferedDebugSink], tmp_path: Path
) -> None:
    """Principal #259 item 4: the packet's url_proof field, from `_loaded`'s own read."""
    from steerai.world.address_bar import DocumentUrl  # noqa: PLC0415 - the read's shape

    bar = url.Bar(url.HOME, url.RESULTS)
    h = url._harness(bar, loaded="Bing")
    h.session._document_url = lambda _hwnd: DocumentUrl(
        runtime_id=(42, 90), url="https://" + (url.RESULTS if bar.pressed else url.HOME)
    )
    h.session.run_step(url._submit())
    records = _decoded(sinks[-1], tmp_path / f"d{len(sinks) - 1}.dpapi")
    proof = next(r for r in records if r["kind"] == "url_proof")
    assert proof["document_url"] == {"url": "https://" + url.RESULTS, "runtime_id": [42, 90]}


def test_an_unreadable_bar_before_the_enter_is_unread_not_a_digest(
    sinks: list[BufferedDebugSink], tmp_path: Path
) -> None:
    h = url._harness(url.Bar(None, url.RESULTS))
    h.session.run_step(url._submit())
    records = _decoded(sinks[-1], tmp_path / f"d{len(sinks) - 1}.dpapi")
    assert next(r for r in records if r["kind"] == "url_proof")["before_url"] == "unread"


_TIMING = frozenset({"latency_ms", "clock", "unchanged_s"})
"""What a slower build may move: the measured times themselves, never a decision."""


def _decisions(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _decisions(v) for k, v in value.items() if k not in _TIMING}
    if isinstance(value, list):
        return [_decisions(v) for v in value]
    return value


class _Ticking(BufferedDebugSink):
    """A sink whose every build costs 5 ms on the session's own fake clock."""

    tick: Any = None

    def capture(self, kind: str, build: Any) -> None:
        if self.tick is not None:
            self.tick(0.005)
        super().capture(kind, build)


@pytest.mark.parametrize("name", sorted(golden.SCENARIOS))
def test_building_records_moves_no_decision(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Invariant 8 (principal #259 item 3): record building that takes time on the fake clock
    leaves every verdict, poll count and settle decision as the golden has them."""
    real_init = loop_mod.Session.__init__
    built: list[_Ticking] = []

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        sink = _Ticking(tmp_path / f"t{len(built)}.dpapi", protect=_keyed)
        sink.tick = self._sleep
        self._debug = sink
        built.append(sink)

    monkeypatch.setattr(loop_mod.Session, "__init__", init)
    fixture = json.loads(golden.FIXTURE.read_text("utf-8"))
    assert _decisions(golden._strip(fixture[name])) == _decisions(golden.SCENARIOS[name]())
    assert any(sink._pending for sink in built), "records were built, so time was spent"
