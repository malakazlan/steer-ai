"""Read a debug trace file: `python -m steerai.probes.trace_view <trace.debug.jsonl.dpapi> [--raw]`.

The file is DPAPI-protected to the Windows user who ran the task (V2 step 1, packet v4 §A). This
reader decrypts it to stdout only, never to a file. What it prints came from pages and apps:
untrusted data, so control characters are escaped (the output is ASCII JSON) and the URLs' query
values are masked unless `--raw`. A line that does not decrypt (another user, another machine,
a run killed mid-write) is counted, never shown in part, and the exit code is 1.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

HEADER = "# untrusted page data: names, titles and URLs below came from pages and apps"


def _unprotect(blob: bytes) -> bytes:
    from steerai.debug_trace import DEBUG_ENTROPY  # noqa: PLC0415 - with the Windows binding
    from steerai.platform import dpapi  # noqa: PLC0415 - ctypes Windows bindings

    return dpapi.unprotect(blob, entropy=DEBUG_ENTROPY)


def decode(raw: bytes, *, unprotect: Callable[[bytes], bytes]) -> tuple[list[dict[str, Any]], int]:
    """The records that decrypt, and how many lines did not."""
    records: list[dict[str, Any]] = []
    failures = 0
    for line in raw.splitlines():
        try:
            record = json.loads(unprotect(base64.b64decode(line, validate=True)).decode("utf-8"))
        except (binascii.Error, OSError, ValueError):
            failures += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            failures += 1
    return records, failures


def _masked(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    if isinstance(value, str) and "?" in value and "://" in value:
        parts = urllib.parse.urlsplit(value)
        if parts.query:
            pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            query = "&".join(f"{key}=*" for key, _ in pairs)
            return urllib.parse.urlunsplit(parts._replace(query=query))
    return value


def render(record: dict[str, Any], *, raw: bool) -> str:
    """One record as an ASCII JSON line: every control character escaped."""
    return json.dumps(record if raw else _masked(record), ensure_ascii=True, sort_keys=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    unprotect: Callable[[bytes], bytes] = _unprotect,
) -> int:
    parser = argparse.ArgumentParser(description="Decrypt a Steer debug trace to stdout.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--raw", action="store_true", help="show URL query values unmasked")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    records, failures = decode(args.path.read_bytes(), unprotect=unprotect)
    stdout.write(HEADER + "\n")
    for record in records:
        stdout.write(render(record, raw=args.raw) + "\n")
    if failures:
        stderr.write(f"{failures} line(s) could not be decrypted (another user, or truncated)\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
