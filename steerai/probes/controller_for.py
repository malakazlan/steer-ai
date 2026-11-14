"""[V2.19] item 3, [V2.15] Q4: what reading UIA ControllerFor costs, and whether Chrome fills it.

    uv run python -m steerai.probes.controller_for

On a local page with a combobox whose `aria-controls` points at a listbox, reads ControllerFor
(UIA_ControllerForPropertyId 30104; Microsoft Learn, "Automation Element Property Identifiers":
variant type for clients is an IUIAutomationElementArray) on the field and reports whether it
names the listbox's RuntimeId. Times, n = 25 each (p50 / p95, ms):

- `standalone`: `CurrentControllerFor` and the RuntimeIds of its elements, one live call;
- `fetch_base`: the cached subtree fetch `page_facts` makes (RuntimeId, AriaRole, HeadingLevel,
  LandmarkType);
- `fetch_with_controller_for`: the same fetch with ControllerFor added to the cache request.

The two fetches are interleaved so a drifting machine hits both. Closed values and numbers only.

Exit 75 when the pre-flight refuses.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from typing import Any

from steerai.probes.memory_guard import EXIT_LOW_MEMORY, FLOOR_MB, LowMemory, refusal_row
from steerai.probes.stats import LatencyStats
from steerai.world.landmarks import RuntimeId

CONTROLLER_FOR = 30104
RUNS = 25
PAGE = """<!doctype html><meta charset="utf-8"><title>Controller</title>
<input id="f-combo" role="combobox" aria-label="City" aria-expanded="true" aria-controls="lb">
<ul id="lb" role="listbox" aria-label="Options"><li role="option">One</li>
<li role="option">Two</li><li role="option">Three</li></ul>
<p id="ready">Controller ready</p>
"""


def names_listbox(controlled: Sequence[RuntimeId], listbox: RuntimeId) -> bool:
    """Whether the field's ControllerFor elements include the listbox."""
    return listbox in controlled


def timing_row(name: str, samples_ms: Sequence[float]) -> dict[str, Any]:  # Any: JSON
    stats = LatencyStats.from_samples(samples_ms)
    return {
        "timing": name,
        "n": stats.n,
        "p50_ms": round(stats.p50_ms, 2),
        "p95_ms": round(stats.p95_ms, 2),
    }


def report(
    controlled: Sequence[RuntimeId],
    listbox: RuntimeId,
    *,
    standalone_ms: Sequence[float],
    base_ms: Sequence[float],
    with_ms: Sequence[float],
) -> list[dict[str, Any]]:  # Any: JSON
    """The rows: whether it names the listbox, the three timings, and what the extra property adds
    to the fetch (p50 and p95 differences)."""
    base = LatencyStats.from_samples(base_ms)
    extra = LatencyStats.from_samples(with_ms)
    return [
        {
            "check": "controller_for_names_listbox",
            "controlled": len(controlled),
            "names_listbox": names_listbox(controlled, listbox),
        },
        timing_row("standalone", standalone_ms),
        timing_row("fetch_base", base_ms),
        timing_row("fetch_with_controller_for", with_ms),
        {
            "report": "controller_for",
            "fetch_extra_p50_ms": round(extra.p50_ms - base.p50_ms, 2),
            "fetch_extra_p95_ms": round(extra.p95_ms - base.p95_ms, 2),
        },
    ]


def _rids(array: Any) -> list[RuntimeId]:  # pragma: no cover - live
    out: list[RuntimeId] = []
    for i in range(array.Length if array else 0):
        rid = array.GetElement(i).GetRuntimeId()
        if rid:
            out.append(tuple(int(v) for v in rid))
    return out


def _fetch_ms(page: Any, *, with_controller: bool) -> float:  # pragma: no cover - live
    mod = page.mod
    request = page.uia.CreateCacheRequest()
    for prop in (
        mod.UIA_RuntimeIdPropertyId,
        mod.UIA_AriaRolePropertyId,
        mod.UIA_HeadingLevelPropertyId,
        30157,  # LANDMARK_TYPE_PROPERTY_ID, as page_facts caches
        *((CONTROLLER_FOR,) if with_controller else ()),
    ):
        request.AddProperty(prop)
    request.TreeScope = mod.TreeScope_Subtree
    element = page.uia.ElementFromHandle(page.hwnd)
    started = time.perf_counter_ns()
    element.BuildUpdatedCache(request)
    return (time.perf_counter_ns() - started) / 1_000_000


def rows_live(floor_mb: int) -> list[dict[str, Any]]:  # pragma: no cover - live  # Any: JSON
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.live_browser import live_page  # noqa: PLC0415 - Windows only
    from steerai.probes.uia_props import _find  # noqa: PLC0415 - Windows only

    with (
        PageServer({"/controller": PAGE}) as server,
        live_page(f"http://127.0.0.1:{server.port}/controller", floor_mb=floor_mb) as page,
    ):
        field = _find(page.uia, page.mod, page.hwnd, "f-combo")
        listbox = _find(page.uia, page.mod, page.hwnd, "lb")
        if not field or not listbox:
            return [{"error": "elements_not_found"}]
        listbox_rid = tuple(int(v) for v in listbox.GetRuntimeId())
        controlled = _rids(field.CurrentControllerFor)
        standalone: list[float] = []
        base: list[float] = []
        extra: list[float] = []
        for _ in range(RUNS):
            started = time.perf_counter_ns()
            _rids(field.CurrentControllerFor)
            standalone.append((time.perf_counter_ns() - started) / 1_000_000)
            base.append(_fetch_ms(page, with_controller=False))
            extra.append(_fetch_ms(page, with_controller=True))
    return report(controlled, listbox_rid, standalone_ms=standalone, base_ms=base, with_ms=extra)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-free-mb", type=int, default=FLOOR_MB)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    try:
        rows = rows_live(args.min_free_mb)
    except LowMemory as refused:
        sys.stdout.write(json.dumps(refusal_row(refused, [])) + "\n")
        sys.stderr.write(f"{refused}\n")
        return EXIT_LOW_MEMORY
    for row in rows:
        sys.stdout.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
