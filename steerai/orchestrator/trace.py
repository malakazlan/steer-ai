"""Task traces (design 5.14, orchestrator review OR-20): one JSON line per event, redacted at
this boundary. A trace carries codes, fixed tokens, ids, counts and latencies; never a name, a
title, a pattern, a goal or message text. Step results and action results are already redacted
by construction (SX-28), the task header carries counts only, and nothing here reads a snapshot.
Because the trace holds no user data it is written in clear; anything that does hold user data
(the ledger) goes through DPAPI.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol


class Trace(Protocol):
    def write(self, record: Mapping[str, Any]) -> None: ...


def _line(record: Mapping[str, Any]) -> str:
    return json.dumps(dict(record), ensure_ascii=False, sort_keys=True)


class ListTrace:
    """Collects lines in memory (tests, and the sentinel scan that proves redaction)."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, record: Mapping[str, Any]) -> None:
        self.lines.append(_line(record))


class JsonLinesTrace:
    """Appends one line per event to a file; the schema is additive only."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: Mapping[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(_line(record) + "\n")


class NullTrace:
    def write(self, _record: Mapping[str, Any]) -> None:
        return None
