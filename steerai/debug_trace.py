"""The debug trace channel (V2 step 1, packet v2-step1-observability v4 §A).

The clear trace (`trace.py`) holds no user data. The debug channel does: element names and
boxes, where a click aimed, the address bar before and after. So it is never written in clear.
Records are built in memory during the task (no I/O and no DPAPI, so nothing here can move a
v1 timer such as the settle cap between a type and its Enter), then each is protected and
written once, at the task's end, as one base64 line. There is no plaintext fallback: a
protector that fails writes nothing.

Isolation (hunter OB-08/09/13/14/15): building or writing a record never raises into the task.
A failure is a closed token (`build_failed`, `write_failed`, `unavailable`) the caller writes to
the clear trace, never an exception message, which could quote a name.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, Literal, Protocol

TraceLevel = Literal["redacted", "debug"]
DebugFailure = Literal["build_failed", "write_failed", "unavailable"]

DEBUG_ENTROPY: Final = b"steer-debug-trace-v1"
"""DPAPI entropy of the debug file: its own, so a ledger or cache blob never opens as one
(OB-42)."""
MAX_STRING: Final = 1000
"""Characters kept of any string, at any depth (a code-point cut; `truncated` counts the
rest): 50 neighbours at the cap stay under `MAX_RECORD_BYTES`."""
MAX_NEIGHBOURS: Final = 50
"""Items kept of any list (a neighbourhood, poll reads)."""
MAX_RECORD_BYTES: Final = 64 * 1024
"""A record larger than this once encoded is replaced by its kind and a token."""

_KIND = re.compile(r"[a-z][a-z_]{0,39}")
_RANK: Final[dict[DebugFailure, int]] = {"build_failed": 0, "write_failed": 1, "unavailable": 2}


class DebugSink(Protocol):
    def begin_step(self, step_id: str) -> None: ...

    def record(self, kind: str, fields: Mapping[str, Any]) -> None: ...

    def capture(self, kind: str, build: Callable[[], Mapping[str, Any]]) -> None: ...

    def flush(self) -> DebugFailure | None: ...


class NullDebugSink:
    """The default: debug is off, nothing is kept."""

    def begin_step(self, step_id: str) -> None:  # noqa: ARG002 - the protocol's shape
        return None

    def record(self, kind: str, fields: Mapping[str, Any]) -> None:  # noqa: ARG002 - protocol
        _check_kind(kind)

    def capture(self, kind: str, build: Callable[[], Mapping[str, Any]]) -> None:  # noqa: ARG002
        """Off: `build` never runs, so debug costs nothing and reads nothing."""
        _check_kind(kind)

    def flush(self) -> DebugFailure | None:
        return None


class BufferedDebugSink:
    """Records kept in memory, protected and appended to `path` by `flush` (task end)."""

    def __init__(self, path: Path, *, protect: Callable[[bytes], bytes]) -> None:
        self._path = path
        self._protect = protect
        self._step_id: str | None = None
        self._seq = 0
        self._pending: list[bytes] = []
        self._failure: DebugFailure | None = None

    def begin_step(self, step_id: str) -> None:
        self._step_id = step_id

    def record(self, kind: str, fields: Mapping[str, Any]) -> None:
        _check_kind(kind)  # a programming error in the caller, raised in tests, not in a run
        self._seq += 1
        try:
            body = _bounded({"kind": kind, "step_id": self._step_id, "seq": self._seq, **fields})
            data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode(
                "utf-8", errors="backslashreplace"
            )
        except (TypeError, ValueError, RecursionError):
            self._fail("build_failed")
            return
        if len(data) > MAX_RECORD_BYTES:
            data = json.dumps(
                {"kind": kind, "step_id": self._step_id, "seq": self._seq, "dropped": "too_large"}
            ).encode("utf-8")
        self._pending.append(data)

    def capture(self, kind: str, build: Callable[[], Mapping[str, Any]]) -> None:
        """Build the record's fields and keep it. Only the building is guarded: a failure is
        the `build_failed` token, never an exception into the step that called it (OB-09)."""
        _check_kind(kind)
        try:
            fields = build()
        except Exception:  # noqa: BLE001 - debug only: a bug here never touches the task
            self._fail("build_failed")
            return
        self.record(kind, fields)

    def flush(self) -> DebugFailure | None:
        """Protect and append every pending record; the failure token, if any, else None."""
        pending, self._pending = self._pending, []
        lines: list[bytes] = []
        for data in pending:
            try:
                lines.append(base64.b64encode(self._protect(data)) + b"\n")
            except OSError:
                self._fail("unavailable")
                return self._failure
        if lines:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("ab") as out:
                    out.write(b"".join(lines))
            except OSError:
                self._fail("write_failed")
        return self._failure

    def _fail(self, token: DebugFailure) -> None:
        if self._failure is None or _RANK[token] > _RANK[self._failure]:
            self._failure = token


def _check_kind(kind: str) -> None:
    if not _KIND.fullmatch(kind):
        raise ValueError(f"a debug record kind is a short lowercase token, got {kind!r}")


def _bounded(record: Mapping[str, Any]) -> dict[str, Any]:
    """The record with every string and list capped, at any depth (principal #259 item 1);
    `truncated` maps each cut field's path (`element.name`, `descendants[0].name`) to what
    went."""
    cut: dict[str, int] = {}
    out = _capped(dict(record), "", cut)
    if cut:
        out["truncated"] = cut
    return out  # type: ignore[no-any-return]  # a dict in, a dict out


def _capped(value: Any, path: str, cut: dict[str, int]) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            cut[path] = len(value) - MAX_STRING
            return value[:MAX_STRING]
        return value
    if isinstance(value, Mapping):
        return {k: _capped(v, f"{path}.{k}" if path else str(k), cut) for k, v in value.items()}
    if isinstance(value, list | tuple):
        if len(value) > MAX_NEIGHBOURS:
            cut[path] = len(value) - MAX_NEIGHBOURS
        return [_capped(v, f"{path}[{n}]", cut) for n, v in enumerate(value[:MAX_NEIGHBOURS])]
    return value


def debug_path_for(trace: Path) -> Path:
    """The debug file beside a trace: `trace.jsonl` -> `trace.debug.jsonl.dpapi`."""
    return trace.with_name(f"{trace.stem}.debug{trace.suffix}.dpapi")


def protect_for_user(data: bytes) -> bytes:
    """DPAPI to the current Windows user with the debug entropy; `OSError` elsewhere."""
    from steerai.platform import dpapi  # noqa: PLC0415 - ctypes Windows bindings

    return dpapi.protect(data, entropy=DEBUG_ENTROPY)
