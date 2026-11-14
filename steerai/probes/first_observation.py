"""[V2.27] the first observation of a fresh Chrome page: does it see the page's tree?

`--measure` (the lead's inference): launch a local page and observe it at once, then at short
intervals, reporting the elements, the top Document's descendants and the fields each time.
`--check` (the live check): the first v2 observation (`observe2.first_observation`) contains
the page's fields. Read-only; one throwaway Chrome behind the memory guard. Windows only.
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
SCHEDULE_S = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0)
"""When `--measure` observes, from the moment the window exists."""
FIELD_ROLES = frozenset({"Edit", "ComboBox"})


def snapshot_row(at_ms: int, snapshot: Snapshot) -> dict[str, Any]:  # Any: JSON
    """One observation as closed counts: elements, the top Document's direct children, fields."""
    top = top_document(snapshot)
    children = 0 if top is None else sum(e.parent_id == top.id for e in snapshot.elements)
    return {
        "at_ms": at_ms,
        "elements": len(snapshot.elements),
        "document": top is not None,
        "document_children": children,
        "fields": sum(e.role in FIELD_ROLES for e in snapshot.elements),
        "stable": snapshot.stable,
    }


def check_row(snapshot: Snapshot, *, expected: int = len(FIELDS)) -> dict[str, Any]:  # Any: JSON
    """The live check: the first v2 observation holds every field of the page."""
    fields = sum(e.role in FIELD_ROLES for e in snapshot.elements)
    row = {"check": "first_observation_has_fields", "fields": fields, "expected": expected}
    return row | ({} if fields >= expected and snapshot.stable else {"error": "fields_missing"})


@contextmanager
def _fresh_page(floor_mb: int) -> Iterator[int]:  # pragma: no cover - live
    """A fresh Chrome on the local page: its window handle, as soon as the window exists."""
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
        url = f"http://127.0.0.1:{server.port}/form"
        proc = guarded_launch(
            launch_chrome, CHROME_DEFAULT, tmp / "profile", _free_port(), url, floor_mb=floor_mb
        )
        try:
            yield wait_window_for_pid(proc.pid, image="chrome.exe", process=proc).hwnd
        finally:
            guarded_stop(stop_chrome, proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def measure_live(floor_mb: int) -> list[dict[str, Any]]:  # pragma: no cover - live
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.world.model import WorldModel  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    with _fresh_page(floor_mb) as hwnd:
        model = WorldModel(UiaClient())
        started = time.monotonic()
        for at in SCHEDULE_S:
            time.sleep(max(0.0, started + at - time.monotonic()))
            snap = model.observe(hwnd)
            rows.append(snapshot_row(round((time.monotonic() - started) * 1000), snap))
    return rows


def check_live(floor_mb: int) -> dict[str, Any]:  # pragma: no cover - live
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.world.model import WorldModel  # noqa: PLC0415
    from steerai.world.observe2 import first_observation, read_observed  # noqa: PLC0415

    with _fresh_page(floor_mb) as hwnd:
        client = UiaClient()
        model = WorldModel(client)
        seen = first_observation(
            lambda: read_observed(model.observe(hwnd), client, key=b"k" * 32, page_facts=False)
        )
        return check_row(seen.snapshot)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-free-mb", type=int, default=FLOOR_MB)
    parser.add_argument("--check", action="store_true", help="the live check, not the measure")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    try:
        rows = [check_live(args.min_free_mb)] if args.check else measure_live(args.min_free_mb)
    except LowMemory as refused:
        sys.stdout.write(json.dumps(refusal_row(refused, [])) + "\n")
        sys.stderr.write(f"{refused}\n")
        return EXIT_LOW_MEMORY
    for row in rows:
        sys.stdout.write(json.dumps(row) + "\n")
    return 1 if any("error" in row for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
