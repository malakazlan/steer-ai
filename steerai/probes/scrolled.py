"""[V2.19] item 3, [V2.18]: how to read the `scrolled` token in Chrome's UIA tree.

    uv run python -m steerai.probes.scrolled

On a local tall page, sends PageDown through DevTools key events (never OS input) until the
bottom. After each press reports whether the top Document's ScrollPattern `VerticalScrollPercent`
is readable and changed, and how many elements flipped their IsOffscreen flag. Stops at the
bottom (percent at or past 99.5), after two presses that move nothing, or at the press cap.
Closed values and counts only; the page text is made up.

Exit 75 when the pre-flight refuses.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from steerai.probes.memory_guard import EXIT_LOW_MEMORY, FLOOR_MB, LowMemory, refusal_row
from steerai.world.landmarks import RuntimeId

PRESS_CAP = 40
BOTTOM_PERCENT = 99.5
SETTLE_S = 0.7
"""Unmeasured: the wait after a press for the scroll and the tree to settle."""
LINES = 120
PAGE = (
    '<!doctype html><meta charset="utf-8"><title>Tall</title>'
    "<style>p{height:90px;margin:0;border-bottom:1px solid #ccc}</style><main>"
    + "".join(f'<p id="p{i}">Line {i}</p>' for i in range(LINES))
    + "</main>"
)


@dataclass(frozen=True, slots=True)
class Press:
    percent: float | None
    """VerticalScrollPercent after the press; None when the pattern is not readable."""
    flipped: int


def flipped(before: Mapping[RuntimeId, bool], after: Mapping[RuntimeId, bool]) -> int:
    """How many elements in both maps changed their IsOffscreen flag; one in only one map is not
    counted (it appeared or went, which is a different fact)."""
    return sum(1 for rid, was in before.items() if rid in after and after[rid] != was)


def stop_reason(presses: Sequence[Press], *, cap: int = PRESS_CAP) -> str | None:
    """Why to stop pressing, or None to press again: `bottom` (percent at the end), `no_movement`
    (the last two presses left the percent equal or unreadable and flipped nothing), `cap`."""
    if presses and presses[-1].percent is not None and presses[-1].percent >= BOTTOM_PERCENT:
        return "bottom"
    if len(presses) >= 2:
        a, b = presses[-2], presses[-1]
        if a.flipped == 0 and b.flipped == 0 and (b.percent is None or b.percent == a.percent):
            return "no_movement"
    return "cap" if len(presses) >= cap else None


def press_row(
    number: int, previous: float | None, current: Press, *, elements: int
) -> dict[str, Any]:  # Any: JSON
    """One press's report row against the reading before it."""
    return {
        "press": number,
        "percent": current.percent,
        "percent_readable": current.percent is not None,
        "percent_changed": (
            current.percent is not None and previous is not None and current.percent != previous
        ),
        "flipped": current.flipped,
        "elements": elements,
    }


def summary(rows: Sequence[Mapping[str, Any]], reason: str | None) -> dict[str, Any]:  # Any: JSON
    """Whether the percent was readable and ever changed, whether flips ever happened, and why
    the run stopped."""
    return {
        "report": "scrolled",
        "presses": len(rows),
        "percent_readable": any(r["percent_readable"] for r in rows),
        "percent_ever_changed": any(r["percent_changed"] for r in rows),
        "flips_ever": any(r["flipped"] > 0 for r in rows),
        "stopped": reason,
    }


def _offscreen(page: Any, document: Any) -> dict[RuntimeId, bool]:  # pragma: no cover - live
    mod = page.mod
    request = page.uia.CreateCacheRequest()
    request.AddProperty(mod.UIA_RuntimeIdPropertyId)
    request.AddProperty(mod.UIA_IsOffscreenPropertyId)
    found = document.FindAllBuildCache(
        mod.TreeScope_Descendants, page.uia.CreateTrueCondition(), request
    )
    out: dict[RuntimeId, bool] = {}
    for i in range(found.Length):
        node = found.GetElement(i)
        rid = node.GetCachedPropertyValue(mod.UIA_RuntimeIdPropertyId)
        if rid:
            out[tuple(int(v) for v in rid)] = bool(
                node.GetCachedPropertyValue(mod.UIA_IsOffscreenPropertyId)
            )
    return out


def _percent(page: Any, document: Any) -> float | None:  # pragma: no cover - live
    mod = page.mod
    pattern = document.GetCurrentPattern(mod.UIA_ScrollPatternId)
    if not pattern:
        return None
    value = float(
        pattern.QueryInterface(mod.IUIAutomationScrollPattern).CurrentVerticalScrollPercent
    )
    return value if value >= 0 else None  # UIA_ScrollPatternNoScroll is -1


def _press_page_down(cdp: Any) -> None:  # pragma: no cover - live
    key = {"key": "PageDown", "code": "PageDown", "windowsVirtualKeyCode": 34}
    cdp.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", **key})
    cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", **key})


def rows_live(floor_mb: int) -> tuple[list[dict[str, Any]], str | None]:  # pragma: no cover - live
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.live_browser import live_page  # noqa: PLC0415 - Windows only

    rows: list[dict[str, Any]] = []
    presses: list[Press] = []
    with (
        PageServer({"/tall": PAGE}) as server,
        live_page(f"http://127.0.0.1:{server.port}/tall", floor_mb=floor_mb) as page,
    ):
        mod = page.mod
        found = page.uia.ElementFromHandle(page.hwnd).FindFirst(
            mod.TreeScope_Descendants,
            page.uia.CreatePropertyCondition(
                mod.UIA_ControlTypePropertyId, mod.UIA_DocumentControlTypeId
            ),
        )
        if not found:
            return [{"error": "no_document"}], None
        page.cdp.call("Runtime.evaluate", {"expression": "window.focus()"})
        offscreen = _offscreen(page, found)
        previous = _percent(page, found)
        while (reason := stop_reason(presses)) is None:
            _press_page_down(page.cdp)
            time.sleep(SETTLE_S)
            after = _offscreen(page, found)
            current = Press(_percent(page, found), flipped(offscreen, after))
            rows.append(press_row(len(presses) + 1, previous, current, elements=len(after)))
            presses.append(current)
            previous, offscreen = current.percent, after
    return rows, reason


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-free-mb", type=int, default=FLOOR_MB)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    try:
        rows, reason = rows_live(args.min_free_mb)
    except LowMemory as refused:
        sys.stdout.write(json.dumps(refusal_row(refused, [])) + "\n")
        sys.stderr.write(f"{refused}\n")
        return EXIT_LOW_MEMORY
    for row in (*rows, summary(rows, reason)):
        sys.stdout.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
