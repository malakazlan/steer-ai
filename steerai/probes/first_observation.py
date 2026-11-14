"""[V2.27], [V2.28] 2: the first observation of a fresh Chrome page: does it see the page's tree?

`--measure` (the lead's inference): launch a local page (or, with `--site`, one pooled H1-H6
task's start page, never H7) and observe it at once, then at intervals, reporting the elements,
the window's node count, the top Document's direct children, its nodes and the fields inside it.
`--check` (the live check): the first v2 observation (`observe2.first_observation`) contains
the page's fields, counted inside the Document only (Chrome's address bar is an Edit too); each
read it made is printed with `stable`, the Document's children and its node count.
Read-only; one throwaway Chrome behind the memory guard. Windows only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from steerai.probes.memory_guard import (
    EXIT_LOW_MEMORY,
    FLOOR_MB,
    LowMemory,
    guarded_launch,
    guarded_stop,
    refusal_row,
)
from steerai.world.effects import top_document
from steerai.world.model import Snapshot
from steerai.world.observe2 import document_nodes

FIELDS = ("q", "email", "notes")
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Form</title></head>
<body><main><h1>Form</h1>
<label>Search <input id="q" type="search"></label>
<label>Email <input id="email" type="text"></label>
<label>Notes <input id="notes" type="text"></label>
<button type="button">Go</button>
</main></body></html>
"""
SCHEDULE_S = tuple(step / 4 for step in range(41))
"""When `--measure` observes, from the moment the window exists: every 0.25 s to the 10 s
deadline (a read that runs late shifts the next; each row says when it began)."""
FIELD_ROLES = frozenset({"Edit", "ComboBox"})


def pooled_site(task_id: str) -> str:
    """[V2.28] 2: the start page of one pooled H1-H6 browser task; ValueError for H7 (sealed), a
    desktop task, the dev set or an unknown id."""
    from steerai.probes.task_suite import H2, H3, H4, H5, H6, HELD_OUT  # noqa: PLC0415

    for task in (*HELD_OUT, *H2, *H3, *H4, *H5, *H6):
        if task.task_id == task_id and task.start.startswith("https://"):
            return task.start
    raise ValueError(f"not_a_pooled_site: {task_id}")


def _counts(snapshot: Snapshot) -> dict[str, Any]:  # Any: JSON
    top = top_document(snapshot)
    inside = document_nodes(snapshot) or frozenset()
    children = 0 if top is None else sum(e.parent_id == top.id for e in snapshot.elements)
    return {
        "stable": snapshot.stable,
        "document": top is not None,
        "document_children": children,
        "document_nodes": len(inside),
        "fields": sum(e.id in inside and e.role in FIELD_ROLES for e in snapshot.elements),
    }


def snapshot_row(at_ms: int, snapshot: Snapshot) -> dict[str, Any]:  # Any: JSON
    """One observation as closed counts: elements, the window's node count, the top Document's
    direct children and nodes, and the fields inside it."""
    return {
        "at_ms": at_ms,
        "elements": len(snapshot.elements),
        "node_count": snapshot.node_count,
        **_counts(snapshot),
    }


def check_row(snapshot: Snapshot, *, expected: int = len(FIELDS)) -> dict[str, Any]:  # Any: JSON
    """The live check: the first v2 observation holds every field of the page, inside its
    Document."""
    counts = _counts(snapshot)
    row = {"check": "first_observation_has_fields", "expected": expected, **counts}
    ok = counts["fields"] >= expected and snapshot.stable
    return row | ({} if ok else {"error": "fields_missing"})


@contextmanager
def _fresh_page(floor_mb: int, site: str | None = None) -> Iterator[int]:  # pragma: no cover
    """A fresh Chrome on the local page, or on `site`: its window handle, as soon as the window
    exists (live)."""
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _free_port  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only

    tmp = Path(tempfile.mkdtemp(prefix="steer-first-"))
    with PageServer({"/form": PAGE}) as server:
        url = site or f"http://127.0.0.1:{server.port}/form"
        proc = guarded_launch(
            launch_chrome, CHROME_DEFAULT, tmp / "profile", _free_port(), url, floor_mb=floor_mb
        )
        try:
            yield wait_window_for_pid(proc.pid, image="chrome.exe", process=proc).hwnd
        finally:
            guarded_stop(stop_chrome, proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def measure_live(floor_mb: int, site: str | None) -> list[dict[str, Any]]:  # pragma: no cover
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.world.model import WorldModel  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    with _fresh_page(floor_mb, site) as hwnd:
        model = WorldModel(UiaClient())
        started = time.monotonic()
        for at in SCHEDULE_S:
            time.sleep(max(0.0, started + at - time.monotonic()))
            began = time.monotonic()
            snap = model.observe(hwnd)
            row = snapshot_row(round((began - started) * 1000), snap)
            rows.append(row | {"read_ms": round((time.monotonic() - began) * 1000)})
    return rows


def check_live(floor_mb: int) -> list[dict[str, Any]]:  # pragma: no cover - live
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.world.effects import Observed  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415
    from steerai.world.observe2 import first_observation, read_observed  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    with _fresh_page(floor_mb) as hwnd:
        client = UiaClient()
        model = WorldModel(client)
        started = time.monotonic()

        def read() -> Observed:
            began = time.monotonic()
            seen = read_observed(model.observe(hwnd), client, key=b"k" * 32, page_facts=False)
            rows.append(snapshot_row(round((began - started) * 1000), seen.snapshot))
            return seen

        seen = first_observation(read)
        return [*rows, check_row(seen.snapshot)]


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-free-mb", type=int, default=FLOOR_MB)
    parser.add_argument("--check", action="store_true", help="the live check, not the measure")
    parser.add_argument("--site", help="--measure on this pooled H1-H6 task's start page")
    args = parser.parse_args(argv)
    try:
        site = None if args.site is None else pooled_site(args.site)
    except ValueError as exc:
        parser.error(str(exc))
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    try:
        rows = check_live(args.min_free_mb) if args.check else measure_live(args.min_free_mb, site)
    except LowMemory as refused:
        sys.stdout.write(json.dumps(refusal_row(refused, [])) + "\n")
        sys.stderr.write(f"{refused}\n")
        return EXIT_LOW_MEMORY
    for row in rows:
        sys.stdout.write(json.dumps(row) + "\n")
    return 1 if any("error" in row for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
