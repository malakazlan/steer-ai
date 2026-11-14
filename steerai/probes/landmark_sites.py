"""[V2.19] item 3: verify the nearest-ancestor landmark walk (#281) on the pooled sites.

    uv run python -m steerai.probes.landmark_sites

For each pooled search site start page (the same set `uia_props --sites` iterates) reads
`UiaClient.page_facts` and, for each visible Edit or ComboBox field, reports its nearest
landmark kind (`none` when it has none, `unobserved` when the field is missing from the map) and
whether any Button shares the field's search landmark (the per-site button check). One Chrome at a
time behind the memory pre-flight. Read-only: no input. Closed values only; no page text.

Exit 75 when the pre-flight refuses.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from steerai.probes.memory_guard import EXIT_LOW_MEMORY, FLOOR_MB, LowMemory, refusal_row
from steerai.world.landmarks import PageFacts, RuntimeId

EDIT, COMBOBOX, BUTTON = 50004, 50003, 50000
_FIELDS = {EDIT: "Edit", COMBOBOX: "ComboBox"}
Element = tuple[RuntimeId, int, bool]
"""(RuntimeId, ControlType id, IsOffscreen) of one element in a Document."""


def landmark_rows(
    elements: Sequence[Element], facts: PageFacts
) -> list[dict[str, Any]]:  # Any: JSON
    """One row per visible Edit or ComboBox: its ControlType name, AriaRole, nearest landmark kind
    and whether a Button's nearest landmark is the same search landmark as the field's."""
    buttons = [rid for rid, kind, _ in elements if kind == BUTTON]
    rows: list[dict[str, Any]] = []
    for rid, kind, offscreen in elements:
        if kind not in _FIELDS or offscreen:
            continue
        observed = rid in facts.landmarks
        landmark = facts.landmarks.get(rid)
        shared = (
            landmark is not None
            and landmark.kind == "search"
            and any(
                (other := facts.landmarks.get(b)) is not None
                and other.kind == "search"
                and other.runtime_id == landmark.runtime_id
                for b in buttons
            )
        )
        rows.append(
            {
                "control_type": _FIELDS[kind],
                "role": facts.roles.get(rid, ""),
                "landmark": landmark.kind if landmark else ("none" if observed else "unobserved"),
                "search_button_shared": shared,
            }
        )
    return rows


def _elements(page: Any) -> list[Element]:  # pragma: no cover - live
    """Every element in the page's Documents, as (RuntimeId, ControlType, IsOffscreen)."""
    uia, mod = page.uia, page.mod
    documents = uia.ElementFromHandle(page.hwnd).FindAll(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, mod.UIA_DocumentControlTypeId),
    )
    out: list[Element] = []
    for d in range(documents.Length):
        found = documents.GetElement(d).FindAll(
            mod.TreeScope_Descendants, uia.CreateTrueCondition()
        )
        for i in range(found.Length):
            element = found.GetElement(i)
            rid = element.GetRuntimeId()
            if rid:
                out.append(
                    (
                        tuple(int(v) for v in rid),
                        int(element.CurrentControlType),
                        bool(element.CurrentIsOffscreen),
                    )
                )
    return out


def site_row(url: str, *, floor_mb: int) -> dict[str, Any]:  # pragma: no cover - live  # Any: JSON
    from steerai.probes.live_browser import live_page  # noqa: PLC0415 - Windows only

    with live_page(url, floor_mb=floor_mb, settle_s=6.0) as page:
        facts = page.client.page_facts(page.hwnd)
        return {"fields": landmark_rows(_elements(page), facts)}


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-free-mb", type=int, default=FLOOR_MB)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    from steerai.probes.task_suite import H2, H3, H4, H5, H6, HELD_OUT, TASKS  # noqa: PLC0415

    seen: set[str] = set()
    for task in (*TASKS, *HELD_OUT, *H2, *H3, *H4, *H5, *H6):
        if not task.start.startswith("https://") or task.start in seen:
            continue
        seen.add(task.start)
        try:
            row = site_row(task.start, floor_mb=args.min_free_mb)
        except LowMemory as refused:
            sys.stdout.write(json.dumps(refusal_row(refused, [task.task_id])) + "\n")
            sys.stderr.write(f"{refused}\n")
            return EXIT_LOW_MEMORY
        except (OSError, TimeoutError, RuntimeError) as exc:
            row = {"error": type(exc).__name__}
        sys.stdout.write(json.dumps({"task": task.task_id, **row}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
