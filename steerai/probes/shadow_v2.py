"""[V2.27] A: the v2-path shadow diagnostic. v1 drives the tasks through `task_suite`; around each
step the v2 full read (`world.observe2.read_observed`: page facts, the two URL reads, focus, the
window list and the acted field's value) is taken before and after, and `world.effects.observe`
compares the pair. Nothing is scored.

    uv run python -m steerai.probes.shadow_v2 --shadow-out <steps.jsonl> --set dev --runs 1 \
        --out <rows.jsonl> [--keep <dir>]

The other arguments go to `task_suite`, which runs in process here (`--in-process`): the hooks
are installed in this process only.

How it hooks v1 without changing it. The session is built through the runner's own seam
(`run.Deps.build_session`), with v1's trace and debug channel wrapped:

- `ShadowDebug.begin_step` takes the before read when v1 starts a step (before any input).
- `ShadowDebug.capture("effect")` takes the after read once v1 has verified the step and logged
  its 2a effect; it carries the acted element's RuntimeId.
- `ShadowTrace` writes v1's `step` record, then emits one shadow row joined with v1's verb,
  delivery and outcome.

Both proxies pass every call through unchanged and first, and a failed read is a token in the
shadow row, never an exception into the step. The extra reads take wall time (`read_ms` per
row); v1's decisions do not read them. Rows carry closed tokens, booleans and counts only: never
a value, a URL, a name or a title. A field value is kept as a keyed digest in memory and only
the comparison leaves.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import secrets
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from steerai.debug_trace import DebugFailure, DebugSink
from steerai.errors import SteerError
from steerai.orchestrator.trace import Trace
from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import (
    EffectReport,
    Observed,
    ValueFact,
    observe,
    top_document,
    value_fact,
)
from steerai.world.landmarks import PageFacts
from steerai.world.model import Snapshot
from steerai.world.observe2 import FactReader, read_observed

if TYPE_CHECKING:
    from steerai.run import Bound, Deps, SessionLike

Row = dict[str, Any]  # Any: JSON values
MAX_FIELDS = 32
"""The editable fields whose value a before read keeps; more are not read (counted)."""
_OBSERVE_FAILURES = (
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    IndexError,
    OverflowError,
    RecursionError,
)
"""As v1's `_log_effect`: the pure observer's own failures become one token in the row."""


class ShadowReads(Protocol):
    """The reads the shadow takes, apart from v1's. Each raises OSError or SteerError on failure."""

    @property
    def facts(self) -> FactReader: ...

    def observe(self, hwnd: int) -> Snapshot: ...

    def windows(self, pid: int) -> tuple[int, ...]: ...

    def value(self, snapshot: Snapshot, element_id: int) -> str | None: ...


@dataclass(slots=True)
class _Read:
    observed: Observed | None
    values: dict[tuple[int, ...], ValueFact]
    ms: float


def _secure(snapshot: Snapshot, element_id: int) -> bool:
    if element_id in snapshot.unread_secure:
        return True
    return any(e.id == element_id and e.secure for e in snapshot.elements)


class V2Shadow:
    """One session's shadow: a before read per step, an after read per verified step, one row
    per step on v1's `step` record."""

    def __init__(
        self,
        reads: ShadowReads,
        *,
        hwnd: int,
        key: bytes,
        emit: Callable[[Row], None],
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._reads, self._hwnd, self._key = reads, hwnd, key
        self._emit, self._clock = emit, clock
        self._before: _Read | None = None
        self._effect = False
        """v1 logged its 2a effect for this step (the after read was taken)."""
        self._after: Row | None = None
        self._errors: list[str] = []
        self._scope: tuple[tuple[int, ...], tuple[int, ...] | None] | None = None
        """The last acted RuntimeId and its top Document, a key press's scope (as v1's)."""

    def before(self) -> None:
        self._effect, self._after, self._errors = False, None, []
        self._before = self._read("before", acted=None)

    def after(self, acted: tuple[int, ...]) -> None:
        before = self._before
        if before is None:
            return
        self._effect = True
        after = self._read("after", acted=acted)
        self._after = self._compare(before, after, acted)

    def finish(self, step_row: Mapping[str, Any]) -> None:
        action = step_row.get("action") or {}
        task_id = str(step_row.get("task_id", ""))
        parts = task_id.split("-")
        row: Row = {
            "task": parts[1] if task_id.startswith("suite-") and len(parts) > 1 else task_id,
            "step_id": step_row.get("step_id"),
            "verb": action.get("verb") if isinstance(action, Mapping) else None,
            "delivery": action.get("delivery") if isinstance(action, Mapping) else None,
            "v1_ok": step_row.get("ok"),
            "effect_row": self._effect,
        }
        row.update(self._after if self._after is not None else _no_report())
        row["errors"] = list(self._errors)
        if self._before is not None:
            row.setdefault("read_ms", {})["before"] = round(self._before.ms, 1)
        self._emit(row)
        self._before, self._effect, self._after = None, False, None

    # ---- reads ---------------------------------------------------------------------------------

    def _read(self, side: str, *, acted: tuple[int, ...] | None) -> _Read:
        started = self._clock()
        try:
            snapshot = self._reads.observe(self._hwnd)
        except (OSError, SteerError) as exc:
            self._errors.append(f"{side}:{type(exc).__name__}")
            return _Read(None, {}, (self._clock() - started) * 1000)
        target = None
        if acted:
            target = next((e.id for e in snapshot.elements if e.runtime_id == acted), None)
        reads = self._reads
        try:
            observed = read_observed(
                snapshot,
                reads.facts,
                key=self._key,
                windows=lambda: reads.windows(snapshot.pid),
                acted_value=None if target is None else partial(reads.value, snapshot, target),
                page_facts=top_document(snapshot) is not None,
                acted=target,
            )
        except (OSError, SteerError) as exc:
            self._errors.append(f"{side}:{type(exc).__name__}")
            return _Read(None, {}, (self._clock() - started) * 1000)
        values = self._values(snapshot) if acted is None else {}
        return _Read(observed, values, (self._clock() - started) * 1000)

    def _values(self, snapshot: Snapshot) -> dict[tuple[int, ...], ValueFact]:
        """Every editable, non-secure field's value as a keyed digest: the acted field is known
        only after the act, so the before keeps them all (at most `MAX_FIELDS`)."""
        out: dict[tuple[int, ...], ValueFact] = {}
        fields = [
            e
            for e in snapshot.elements
            if "editable" in e.caps and e.runtime_id and not _secure(snapshot, e.id)
        ]
        for element in fields[:MAX_FIELDS]:
            try:
                text = self._reads.value(snapshot, element.id)
            except (OSError, SteerError) as exc:
                self._errors.append(f"before_value:{type(exc).__name__}")
                continue
            if text is not None:
                out[element.runtime_id] = value_fact(text, key=self._key)
        return out

    def _compare(self, before: _Read, after: _Read, acted: tuple[int, ...]) -> Row | None:
        if before.observed is None or after.observed is None:
            return None
        b = dataclasses.replace(before.observed, acted_value=before.values.get(acted))
        a = after.observed
        top = top_document(b.snapshot)
        top_rid = None if top is None else top.runtime_id
        scope = None
        if acted:
            self._scope = (acted, top_rid)
        elif self._scope is not None and self._scope[1] == top_rid:
            scope = self._scope[0]
        documents = top is not None or top_document(a.snapshot) is not None
        try:
            report = observe(b, a, acted=acted or None, scope=scope, url_fact=documents)
        except _OBSERVE_FAILURES as exc:
            self._errors.append(f"observe:{type(exc).__name__}")
            return None
        return {
            **_report_row(report),
            "acted": bool(acted),
            "acted_value_read": [b.acted_value is not None, a.acted_value is not None],
            "focus_read": [b.focus is not None, a.focus is not None],
            "url_read": [b.url is not None, a.url is not None],
            "document_url_read": [b.document_url is not None, a.document_url is not None],
            "url_observed": [b.url_observed, a.url_observed],
            "page_facts_read": [b.aria_roles is not None, a.aria_roles is not None],
            "read_ms": {"after": round(after.ms, 1)},
        }


def _report_row(report: EffectReport) -> Row:
    classes = report.classes
    return {
        "classes": sorted(classes),
        "tokens": sorted(report.tokens),
        "unread": sorted(report.unread),
        "primary": report.primary,
        "own_value": "own_value" in classes,
        "focus": "focus" in classes,
        "unknown": "unknown" in classes,
    }


def _no_report() -> Row:
    return {"classes": None, "tokens": None, "unread": None, "primary": None}


# ---- the proxies on v1's own channels ----------------------------------------------------------


class ShadowTrace:
    """v1's trace, unchanged; a `step` record also ends the shadow's step."""

    def __init__(self, inner: Trace, shadow: V2Shadow) -> None:
        self._inner, self._shadow = inner, shadow

    def write(self, record: Mapping[str, Any]) -> None:
        self._inner.write(record)
        if record.get("kind") == "step":
            self._shadow.finish(record)


class ShadowDebug:
    """v1's debug channel, unchanged; a step's start is the before read, its `effect` capture
    the after read."""

    def __init__(self, inner: DebugSink, shadow: V2Shadow) -> None:
        self._inner, self._shadow = inner, shadow

    def begin_step(self, step_id: str) -> None:
        self._inner.begin_step(step_id)
        self._shadow.before()

    def record(self, kind: str, fields: Mapping[str, Any]) -> None:
        self._inner.record(kind, fields)

    def capture(self, kind: str, build: Callable[[], Mapping[str, Any]]) -> None:
        self._inner.capture(kind, build)
        if kind == "effect":
            acted = build().get("acted") or ()
            self._shadow.after(tuple(int(v) for v in acted))

    def flush(self) -> DebugFailure | None:
        return self._inner.flush()


def shadowed(
    build: Callable[[Bound], SessionLike], make: Callable[[Bound], V2Shadow]
) -> Callable[[Bound], SessionLike]:
    """`build` with the session's trace and debug channel wrapped by a new shadow."""

    def build_session(bound: Bound) -> SessionLike:
        shadow = make(bound)
        wrapped = dataclasses.replace(
            bound, trace=ShadowTrace(bound.trace, shadow), debug=ShadowDebug(bound.debug, shadow)
        )
        return build(wrapped)

    return build_session


@contextlib.contextmanager
def installed(make: Callable[[Bound], V2Shadow]) -> Iterator[None]:
    """For this process only: every session `run._real_deps` builds is shadowed. `task_suite`
    builds its deps from `run._real_deps` at each task, so its in-process run is shadowed."""
    from steerai import run as run_mod  # noqa: PLC0415 - the live entry only

    original = run_mod._real_deps

    def deps() -> Deps:
        real = original()
        return real.replace(build_session=shadowed(real.build_session, make))

    run_mod._real_deps = deps
    try:
        yield
    finally:
        run_mod._real_deps = original


# ---- the report --------------------------------------------------------------------------------


def summary(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """[V2.27] A's questions, counted over the shadow rows."""
    seen = [r for r in rows if r.get("classes") is not None]

    def verified(verb: str) -> list[Mapping[str, Any]]:
        return [r for r in seen if r["verb"] == verb and r["delivery"] == "delivered_verified"]

    def having(group: Sequence[Mapping[str, Any]], name: str) -> int:
        return sum(1 for r in group if name in r["classes"])

    def unread(group: Sequence[Mapping[str, Any]], fact: str) -> int:
        return sum(1 for r in group if fact in r["unread"])

    types, focuses = verified("type"), verified("focus")
    presses = [r for r in seen if r["verb"] == "press"]
    not_read = sum(1 for r in presses if not r["url_read"][1])
    return [
        f"verified type: {len(types)}, own_value {having(types, 'own_value')}",
        f"verified focus: {len(focuses)}, focus {having(focuses, 'focus')}",
        f"unknown: {having(seen, 'unknown')} of {len(seen)} observed",
        f"unread: url {unread(seen, 'url')}, windows {unread(seen, 'windows')}, "
        f"caps {unread(seen, 'caps')} (of {len(seen)} observed)",
        f"press: {len(presses)}, url unread {unread(presses, 'url')}, "
        f"url not read after {not_read}, navigated {having(presses, 'navigated')}",
        f"no report: {len(rows) - len(seen)}",
    ]


# ---- live --------------------------------------------------------------------------------------


class _ComFacts:  # pragma: no cover - live, Windows only
    """`UiaClient` as a `FactReader` whose COM failures are all OSError, so `read_observed`
    reads them as not read and none reaches v1's step (`focused_runtime_id` and `page_facts`
    make COM calls outside their own wrapping)."""

    def __init__(self, client: FactReader) -> None:
        self._client = client

    def document_url_in(self, snapshot: Snapshot, document_id: int) -> DocumentUrl | None:
        return _com(lambda: self._client.document_url_in(snapshot, document_id))

    def address_bar(self, hwnd: int) -> str | None:
        return _com(lambda: self._client.address_bar(hwnd))

    def focused_runtime_id(self) -> tuple[int, ...]:
        return _com(self._client.focused_runtime_id)

    def page_facts(self, hwnd: int) -> PageFacts:
        return _com(lambda: self._client.page_facts(hwnd))


def _com[T](read: Callable[[], T]) -> T:  # pragma: no cover - live, Windows only
    from _ctypes import COMError  # noqa: PLC0415 - CPython on Windows

    try:
        return read()
    except COMError as exc:
        raise OSError("uia read failed") from exc


class _UiaReads:  # pragma: no cover - live, Windows only
    """The shadow's own UIA client, model and value reader (never v1's)."""

    def __init__(self) -> None:
        from steerai.action.execution import UiaBindings  # noqa: PLC0415 - Windows only
        from steerai.action.live_reads import UiaLiveReads  # noqa: PLC0415
        from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
        from steerai.world.model import WorldModel  # noqa: PLC0415

        client = UiaClient()
        self._facts = _ComFacts(client)
        self._model = WorldModel(client)
        self._live = UiaLiveReads(client, UiaBindings.load())

    @property
    def facts(self) -> FactReader:
        return self._facts

    def observe(self, hwnd: int) -> Snapshot:
        return _com(lambda: self._model.observe(hwnd, cap_s=5.0))

    def windows(self, pid: int) -> tuple[int, ...]:
        from steerai.probes.windows import list_top_windows  # noqa: PLC0415 - Windows only

        return tuple(sorted(w.hwnd for w in list_top_windows() if w.pid == pid))

    def value(self, snapshot: Snapshot, element_id: int) -> str | None:
        try:
            return _com(lambda: self._live.value(snapshot.handle(element_id)))
        except SteerError as exc:
            raise OSError("value unreadable") from exc


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shadow-out", type=Path, required=True, help="one row per step")
    args, rest = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    from steerai.probes import task_suite  # noqa: PLC0415 - the live entry only

    args.shadow_out.parent.mkdir(parents=True, exist_ok=True)
    with args.shadow_out.open("w", encoding="utf-8") as out:

        def emit(row: Row) -> None:
            out.write(json.dumps(row) + "\n")
            out.flush()

        def make(bound: Bound) -> V2Shadow:
            return V2Shadow(_UiaReads(), hwnd=bound.hwnd, key=secrets.token_bytes(32), emit=emit)

        with installed(make):
            code = task_suite.main([*rest, "--in-process"])
    rows = [json.loads(x) for x in args.shadow_out.read_text("utf-8").splitlines() if x.strip()]
    sys.stdout.write("v2-path shadow ([V2.27] A)\n" + "\n".join(summary(rows)) + "\n")
    return code


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
