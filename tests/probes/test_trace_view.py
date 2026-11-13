"""The debug file's reader (V2 step 1 packet v4 §A; hunter OB-07/29/30/32): decrypts to stdout
only, on request; masks URL query values unless `--raw`; escapes control characters; frames the
output as untrusted page data; counts undecryptable or truncated lines and exits non-zero."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from steerai.probes.trace_view import decode, main, render


def _keyed(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


def _line(record: dict[str, object]) -> bytes:
    return base64.b64encode(_keyed(json.dumps(record).encode("utf-8"))) + b"\n"


def _unkeyed(blob: bytes) -> bytes:
    data = _keyed(blob)
    json.loads(data)  # a wrong key decodes to garbage: refused like DPAPI would
    return data


def test_lines_decode_and_bad_or_truncated_lines_are_counted() -> None:
    good = _line({"kind": "aim", "seq": 1})
    raw = good + b"not base64!!\n" + _line({"kind": "aim", "seq": 2})[:-9]
    records, failures = decode(raw, unprotect=_unkeyed)
    assert [r["seq"] for r in records] == [1]
    assert failures == 2


def test_query_values_are_masked_unless_raw() -> None:
    record = {"kind": "url_proof", "reads": ["https://ex.test/search?q=secret+words&x=1"]}
    assert "secret" not in render(record, raw=False)
    assert "q=*" in render(record, raw=False)
    assert "secret" in render(record, raw=True)


def test_control_characters_never_reach_the_terminal() -> None:
    shown = render({"kind": "aim", "name": "a\x1b[31mred\x07"}, raw=True)
    assert "\x1b" not in shown
    assert "\x07" not in shown
    assert shown.isascii()


def test_main_frames_the_output_and_fails_on_any_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "trace.debug.jsonl.dpapi"
    path.write_bytes(_line({"kind": "aim", "seq": 1}))
    out, err = io.StringIO(), io.StringIO()
    assert main([str(path)], stdout=out, stderr=err, unprotect=_unkeyed) == 0
    assert out.getvalue().startswith("# untrusted page data")
    path.write_bytes(_line({"kind": "aim", "seq": 1}) + b"garbage\n")
    out, err = io.StringIO(), io.StringIO()
    assert main([str(path)], stdout=out, stderr=err, unprotect=_unkeyed) == 1
    assert "1 line(s) could not be decrypted" in err.getvalue()
