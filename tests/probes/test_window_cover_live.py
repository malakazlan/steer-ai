"""[V2.62] the live cover read: read-only Win32 calls on the desktop's own windows (no input, no
window moved). The probe `occlusion` measured the rule; this pins that the read returns facts."""

from __future__ import annotations

import pytest

from steerai.probes.windows import list_top_windows, window_cover
from steerai.world.cover import cover_of

pytestmark = pytest.mark.windows


def test_the_front_window_reads_its_cover_facts() -> None:
    shown = [w for w in list_top_windows() if not w.minimized and not w.cloak_unreadable]
    if not shown:
        pytest.fail("no visible top-level window to read")
    facts = window_cover(shown[0].hwnd)
    assert facts.minimized is False
    assert facts.rect[2] > facts.rect[0]
    assert facts.rect[3] > facts.rect[1]
    assert all(r[2] > r[0] and r[3] > r[1] for r in facts.above)
    assert cover_of(facts) in (None, "covered", "cloaked")
