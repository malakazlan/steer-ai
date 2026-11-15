"""[V2.62] whether the bound window is covered, from facts read once (pure; the Win32 reads are
`probes.windows.window_cover`).

A covered window exposes no page to UIA: Chrome's native window occlusion treats a window whose
whole rectangle other windows cover like a background tab (Chromium
`docs/windows_native_window_occlusion_tracking.md`), and a minimised one is marked hidden. The
probe `probes.occlusion` (2026-11-15, 5 runs per case) measured it: fully covered and never read,
no Document for 10 s; minimised, an empty Document; the same full cover with
`--disable-features=CalculateNativeWinOcclusion`, the page within 0.55 s; background but visible
and 52 % covered, the page at once. So the check is narrow:

- minimised (`IsIconic`);
- cloaked (DWMWA_CLOAKED), or that fact unread (fail closed);
- the union of the window rects of the visible, uncloaked, non-minimised top-level windows above
  it in z-order covers its whole window rect, as an exact area (a few pixels left uncovered keep
  the page, so the area is never sampled).

Not being the foreground window is not a cover.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

Rect = tuple[int, int, int, int]
"""(left, top, right, bottom) in physical pixels."""
CoverToken = Literal["minimized", "cloaked", "covered"]


@dataclass(frozen=True, slots=True)
class CoverFacts:
    """One read of the window's cover facts."""

    minimized: bool
    cloaked: bool | None
    """None: DWM did not answer."""
    rect: Rect
    """The window's own rect (GetWindowRect)."""
    above: tuple[Rect, ...]
    """The window rects of the visible, uncloaked, non-minimised top-level windows above it."""


def covered_fraction(target: Rect, above: Sequence[Rect]) -> float:
    """The exact share of `target` under the union of `above`: 0.0 uncovered, 1.0 fully covered.
    Every edge is clipped to the target; each cell between consecutive edges is wholly inside
    or wholly outside each rect, so the sum is exact."""
    left, top, right, bottom = target
    if right <= left or bottom <= top:
        return 0.0
    clipped = [
        (max(a[0], left), max(a[1], top), min(a[2], right), min(a[3], bottom)) for a in above
    ]
    clipped = [c for c in clipped if c[2] > c[0] and c[3] > c[1]]
    xs = sorted({left, right, *(c[0] for c in clipped), *(c[2] for c in clipped)})
    ys = sorted({top, bottom, *(c[1] for c in clipped), *(c[3] for c in clipped)})
    hit = 0
    for x0, x1 in pairwise(xs):
        for y0, y1 in pairwise(ys):
            if any(c[0] <= x0 and x1 <= c[2] and c[1] <= y0 and y1 <= c[3] for c in clipped):
                hit += (x1 - x0) * (y1 - y0)
    return hit / ((right - left) * (bottom - top))


def cover_of(facts: CoverFacts) -> CoverToken | None:
    """Why the window is covered, or None when it is not. A window with no area is covered."""
    if facts.minimized:
        return "minimized"
    if facts.cloaked is not False:
        return "cloaked"
    left, top, right, bottom = facts.rect
    if right <= left or bottom <= top:
        return "covered"
    if covered_fraction(facts.rect, facts.above) >= 1.0:
        return "covered"
    return None
