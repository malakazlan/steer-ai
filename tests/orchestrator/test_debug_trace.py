"""The debug channel's sink (V2 step 1 packet v4 §A): records built in memory during the task,
protected and written once at its end, one base64 line per record, never in clear. A failure
is a closed token for the clear trace, never an exception into the task (hunter OB-08/13/15)
and never message text."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from steerai.debug_trace import (
    MAX_NEIGHBOURS,
    MAX_STRING,
    BufferedDebugSink,
    NullDebugSink,
)

SENTINEL = "sentinel-Zq8 page text"


def _keyed(data: bytes) -> bytes:
    """A keyed stand-in for DPAPI: no plaintext survives it (OB-34)."""
    return bytes(b ^ 0x5A for b in data)


def _sink(tmp_path: Path, protect: object = _keyed) -> tuple[BufferedDebugSink, Path]:
    path = tmp_path / "trace.debug.jsonl.dpapi"
    return BufferedDebugSink(path, protect=protect), path  # type: ignore[arg-type]  # fakes


def _lines(path: Path) -> list[dict[str, object]]:
    out = []
    for line in path.read_bytes().splitlines():
        out.append(json.loads(_keyed(base64.b64decode(line)).decode("utf-8")))
    return out


def test_nothing_is_written_until_the_task_ends(tmp_path: Path) -> None:
    sink, path = _sink(tmp_path)
    sink.begin_step("s1")
    sink.record("aim", {"name": SENTINEL})
    assert not path.exists(), "no I/O during the task (re-review item 1)"
    assert sink.flush() is None
    assert _lines(path) == [{"kind": "aim", "step_id": "s1", "seq": 1, "name": SENTINEL}]


def test_each_record_is_one_ascii_line_and_no_plaintext_survives(tmp_path: Path) -> None:
    sink, path = _sink(tmp_path)
    sink.begin_step("s1")
    for n in range(3):
        sink.record("url_proof", {"url": f"https://example.test/?q={SENTINEL}{n}\n\r"})
    sink.flush()
    raw = path.read_bytes()
    assert raw.count(b"\n") == 3, "base64 framing: a blob byte is never a line break (OB-16)"
    assert raw.isascii()
    assert SENTINEL.encode() not in raw


def test_the_identity_protector_would_fail_the_scan(tmp_path: Path) -> None:
    """The mutation check (OB-34): the scan above is only meaningful because it can fail."""
    sink, path = _sink(tmp_path, protect=lambda data: data)
    sink.begin_step("s1")
    sink.record("aim", {"name": SENTINEL})
    sink.flush()
    assert SENTINEL.encode() in base64.b64decode(path.read_bytes().splitlines()[0])


def test_a_protector_that_fails_is_a_token_never_an_exception(tmp_path: Path) -> None:
    def broken(_data: bytes) -> bytes:
        raise OSError(f"CryptProtectData failed for {SENTINEL}")

    sink, path = _sink(tmp_path, protect=broken)
    sink.begin_step("s1")
    sink.record("aim", {"name": SENTINEL})
    assert sink.flush() == "unavailable"
    assert not path.exists() or SENTINEL.encode() not in path.read_bytes()


def test_a_record_that_cannot_be_built_is_a_token(tmp_path: Path) -> None:
    sink, path = _sink(tmp_path)
    sink.begin_step("s1")
    sink.record("aim", {"thing": object()})  # not JSON: a bug in the caller, never a crash
    sink.record("aim", {"ok": 1})
    assert sink.flush() == "build_failed"
    assert [r["ok"] for r in _lines(path)] == [1]


def test_a_lone_surrogate_is_escaped_not_a_crash(tmp_path: Path) -> None:
    sink, path = _sink(tmp_path)
    sink.begin_step("s1")
    sink.record("aim", {"name": "a\ud800b"})
    assert sink.flush() is None
    assert _lines(path)[0]["name"] == "a" + chr(0xD800) + "b", "kept, escaped on the way"


def test_strings_and_lists_are_capped_with_a_count(tmp_path: Path) -> None:
    sink, path = _sink(tmp_path)
    sink.begin_step("s1")
    sink.record(
        "neighbourhood",
        {
            "name": "x" * (MAX_STRING + 10),
            "elements": [{"id": n} for n in range(MAX_NEIGHBOURS + 7)],
        },
    )
    sink.flush()
    (record,) = _lines(path)
    assert record["name"] == "x" * MAX_STRING
    assert record["truncated"] == {"name": 10, "elements": 7}
    assert len(record["elements"]) == MAX_NEIGHBOURS  # type: ignore[arg-type]  # a list


def test_the_null_sink_does_nothing(tmp_path: Path) -> None:
    sink = NullDebugSink()
    sink.begin_step("s1")
    sink.record("aim", {"name": SENTINEL})
    assert sink.flush() is None


def test_a_second_flush_writes_nothing_twice(tmp_path: Path) -> None:
    sink, path = _sink(tmp_path)
    sink.begin_step("s1")
    sink.record("aim", {"n": 1})
    sink.flush()
    sink.flush()
    assert len(_lines(path)) == 1


@pytest.mark.parametrize("kind", ["Aim", "aim record", "", "x" * 41])
def test_a_record_kind_is_a_short_token(tmp_path: Path, kind: str) -> None:
    sink, _path = _sink(tmp_path)
    with pytest.raises(ValueError, match="kind"):
        sink.record(kind, {})


@pytest.mark.windows
def test_the_real_protector_round_trips_only_with_the_debug_entropy(tmp_path: Path) -> None:
    from steerai.debug_trace import DEBUG_ENTROPY, protect_for_user  # noqa: PLC0415
    from steerai.platform import dpapi  # noqa: PLC0415

    sink = BufferedDebugSink(tmp_path / "d.dpapi", protect=protect_for_user)
    sink.begin_step("s1")
    sink.record("aim", {"name": SENTINEL})
    assert sink.flush() is None
    (line,) = (tmp_path / "d.dpapi").read_bytes().splitlines()
    blob = base64.b64decode(line)
    assert SENTINEL.encode() not in blob
    assert SENTINEL in dpapi.unprotect(blob, entropy=DEBUG_ENTROPY).decode("utf-8")
    with pytest.raises(OSError, match="CryptUnprotectData failed"):
        dpapi.unprotect(blob, entropy=b"steer-ledger")


def test_capture_builds_only_when_debug_is_on(tmp_path: Path) -> None:
    calls: list[int] = []

    def build() -> dict[str, object]:
        calls.append(1)
        return {"n": 1}

    NullDebugSink().capture("aim", build)
    assert calls == [], "off: nothing is read or built"
    sink, path = _sink(tmp_path)
    sink.begin_step("s1")
    sink.capture("aim", build)
    sink.flush()
    assert calls == [1]
    assert _lines(path)[0]["n"] == 1


def test_a_build_that_raises_is_a_token_and_the_caller_goes_on(tmp_path: Path) -> None:
    sink, _path = _sink(tmp_path)

    def build() -> dict[str, object]:
        raise KeyError(SENTINEL)

    sink.capture("aim", build)
    assert sink.flush() == "build_failed"
