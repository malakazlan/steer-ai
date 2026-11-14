"""[V2.28] before stage 2 (a): what a full v2 read costs per site, part by part.

`read_observed` takes a snapshot, the page facts (AriaRoles, headings, landmarks), the two URL
reads and focus. Live run 2 saw one after-read take 3.9 s on a real page against 53-81 ms on a
local one. This times each part on every pooled site (n reads each, never the sealed H7 pages),
and times the page facts twice: over the whole window (what `read_observed` does) and scoped to
the top Document's subtree, to show whether scoping removes the cost. `observe2` is not changed;
the numbers go in the report.

    uv run python -m steerai.probes.read_cost [--reads 6] [--tasks wk1,gh1] [--out rows.jsonl]

Read-only (no input). One throwaway Chrome at a time behind the memory guard. Windows only.
Percentiles use the probe convention: sorted[n // 2] and sorted[int(n * 0.95)].
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from steerai.probes.memory_guard import EXIT_LOW_MEMORY
from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import top_document
from steerai.world.landmarks import PageFacts
from steerai.world.model import Snapshot

Row = dict[str, Any]  # Any: JSON values
PARTS = ("snapshot", "document_url", "address_bar", "focus", "page_facts", "page_facts_scoped")
"""The timed parts. `page_facts` and the scoped one swap places on alternate reads so neither
always runs second on a warm provider."""
MIN_READS = 5


class CostReads(Protocol):
    """The reads one timed iteration makes; each raises OSError when it fails."""

    def observe(self) -> Snapshot: ...

    def document_url_in(self, snapshot: Snapshot, document_id: int) -> DocumentUrl | None: ...

    def address_bar(self) -> str | None: ...

    def focused_runtime_id(self) -> tuple[int, ...]: ...

    def page_facts(self) -> PageFacts: ...

    def page_facts_in(self, snapshot: Snapshot, document_id: int) -> PageFacts: ...


def percentiles(values: Sequence[float]) -> Row:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": round(ordered[len(ordered) // 2], 1),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "max": round(ordered[-1], 1),
    }


def _timed[T](read: Callable[[], T], clock: Callable[[], float]) -> tuple[T | None, float, bool]:
    """The read's result (None when it raised OSError), its milliseconds, and whether it raised."""
    started = clock()
    try:
        out: T | None = read()
        raised = False
    except OSError:
        out, raised = None, True
    return out, (clock() - started) * 1000, raised


def one_read(reads: CostReads, *, flip: bool, clock: Callable[[], float]) -> Row:
    """One iteration: the snapshot, then the other parts. `ms` maps each part to its time;
    `failed` lists the parts that raised; `scoped_equal` is whether every fact the scoped fetch
    found equals the whole-window fetch's (the scoping lost nothing inside the Document)."""
    ms: dict[str, float] = {}
    failed: list[str] = []
    snapshot, ms["snapshot"], raised = _timed(reads.observe, clock)
    if snapshot is None or raised:
        return {"ms": ms, "failed": ["snapshot"], "elements": 0, "scoped_equal": None}
    top = top_document(snapshot)
    results: dict[str, Any] = {}  # Any: each part's own result type, only compared below

    def part(name: str, read: Callable[[], Any]) -> None:  # Any: see `results`
        results[name], ms[name], raised = _timed(read, clock)
        if raised:
            failed.append(name)

    if top is not None:
        part("document_url", lambda: reads.document_url_in(snapshot, top.id))
    part("address_bar", reads.address_bar)
    part("focus", reads.focused_runtime_id)
    facts: list[tuple[str, Callable[[], Any]]] = [("page_facts", reads.page_facts)]
    if top is not None:
        facts.append(("page_facts_scoped", lambda: reads.page_facts_in(snapshot, top.id)))
    for name, read in reversed(facts) if flip else facts:
        part(name, read)
    return {
        "ms": ms,
        "failed": failed,
        "elements": len(snapshot.elements),
        "scoped_equal": _scoped_equal(results.get("page_facts"), results.get("page_facts_scoped")),
    }


def _scoped_equal(whole: PageFacts | None, scoped: PageFacts | None) -> bool | None:
    if whole is None or scoped is None:
        return None
    return (
        all(whole.roles.get(k) == v for k, v in scoped.roles.items())
        and all(whole.headings.get(k) == v for k, v in scoped.headings.items())
        and all(whole.landmarks.get(k) == v for k, v in scoped.landmarks.items())
    )


def measure_site(
    task: str,
    reads: CostReads,
    *,
    n: int = MIN_READS,
    clock: Callable[[], float] = time.perf_counter,
) -> Row:
    """`n` timed reads on one site's page (the caller has waited for the page): p50, p95 and max
    per part; `full` is the parts a `read_observed` makes (the whole-window page facts) and
    `full_scoped` the same read with the scoped page facts."""
    if n < MIN_READS:
        raise ValueError(f"at least {MIN_READS} reads per site, got {n}")
    samples = [one_read(reads, flip=i % 2 == 1, clock=clock) for i in range(n)]
    parts: dict[str, list[float]] = {name: [] for name in PARTS}
    full: list[float] = []
    scoped: list[float] = []
    for s in samples:
        ms: Mapping[str, float] = s["ms"]
        for name in PARTS:
            if name in ms and name not in s["failed"]:
                parts[name].append(ms[name])
        base = sum(v for k, v in ms.items() if k != "page_facts_scoped")
        full.append(base)
        scoped.append(base - ms.get("page_facts", 0.0) + ms.get("page_facts_scoped", 0.0))
    return {
        "task": task,
        "reads": n,
        "elements": samples[-1]["elements"],
        "parts": {name: percentiles(v) for name, v in parts.items() if v},
        "full": percentiles(full),
        "full_scoped": percentiles(scoped),
        "failed": sorted({f for s in samples for f in s["failed"]}),
        "scoped_equal": all(s["scoped_equal"] is not False for s in samples),
        "scoped_compared": sum(1 for s in samples if s["scoped_equal"] is not None),
    }


def summary(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """One line per site: p50/p95 of the whole read, the page facts and their scoped version."""
    lines = []
    for r in rows:
        if "error" in r:
            lines.append(f"{r['task']}: error {r['error']}")
            continue
        p = r["parts"]
        facts = p.get("page_facts", {})
        scoped = p.get("page_facts_scoped", {})
        lines.append(
            f"{r['task']}: elements {r['elements']}, full p50 {r['full']['p50']} "
            f"p95 {r['full']['p95']} ms; page_facts p50 {facts.get('p50')} p95 {facts.get('p95')}; "
            f"scoped p50 {scoped.get('p50')} p95 {scoped.get('p95')}; "
            f"scoped_equal {r['scoped_equal']}"
        )
    return lines


class _LiveReads:  # pragma: no cover - live, Windows only
    """One page's reads through its own UIA client and model."""

    def __init__(self, client: Any, hwnd: int) -> None:  # Any: UiaClient, Windows only
        from steerai.world.model import WorldModel  # noqa: PLC0415 - Windows only

        self._client, self._hwnd = client, hwnd
        self._model = WorldModel(client)

    def observe(self) -> Snapshot:
        from _ctypes import COMError  # noqa: PLC0415 - CPython on Windows

        try:
            return self._model.observe(self._hwnd, cap_s=5.0)
        except COMError as exc:
            raise OSError("uia read failed") from exc

    def document_url_in(self, snapshot: Snapshot, document_id: int) -> DocumentUrl | None:
        found: DocumentUrl | None = self._client.document_url_in(snapshot, document_id)
        return found

    def address_bar(self) -> str | None:
        bar: str | None = self._client.address_bar(self._hwnd)
        return bar

    def focused_runtime_id(self) -> tuple[int, ...]:
        focus: tuple[int, ...] = self._client.focused_runtime_id()
        return focus

    def page_facts(self) -> PageFacts:
        facts: PageFacts = self._client.page_facts(self._hwnd)
        return facts

    def page_facts_in(self, snapshot: Snapshot, document_id: int) -> PageFacts:
        facts: PageFacts = self._client.page_facts_in(snapshot, document_id)
        return facts


def _site(task: str, url: str, n: int) -> Row:  # pragma: no cover - live
    from steerai.probes.live_browser import TreeNotReady, live_page  # noqa: PLC0415
    from steerai.probes.memory_guard import FLOOR_MB  # noqa: PLC0415

    try:
        with live_page(url, floor_mb=FLOOR_MB, settle_s=6.0) as page:
            reads = _LiveReads(page.client, page.hwnd)
            reads.observe()  # warm-up: the first touch builds Chrome's tree; not measured
            return measure_site(task, reads, n=n)
    except (OSError, TreeNotReady, TimeoutError) as exc:
        return {"task": task, "error": type(exc).__name__}


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reads", type=int, default=6)
    parser.add_argument("--tasks", default="", help="comma list of task ids; default every pooled")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    from steerai.probes.live_browser import exit_code  # noqa: PLC0415
    from steerai.probes.memory_guard import LowMemory  # noqa: PLC0415
    from steerai.probes.task_suite import (  # noqa: PLC0415
        H3,
        H4,
        H5,
        H6,
        HELD_OUT,
        results_url,
    )

    wanted = {t for t in args.tasks.split(",") if t}
    rows: list[Row] = []
    for task in (*HELD_OUT, *H3, *H4, *H5, *H6):  # never H7: it is sealed
        if task.check not in ("url_search", "wiki_article") or (
            wanted and task.task_id not in wanted
        ):
            continue
        try:
            row = _site(task.task_id, results_url(task), args.reads)
        except LowMemory as refused:
            sys.stdout.write(json.dumps({"refused": str(refused)}) + "\n")
            return EXIT_LOW_MEMORY
        rows.append(row)
        sys.stdout.write(json.dumps(row) + "\n")
        sys.stdout.flush()
    if args.out:
        Path(args.out).write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n"
        )
    sys.stdout.write("\n".join(summary(rows)) + "\n")
    return exit_code(rows)


if __name__ == "__main__":
    raise SystemExit(main())
