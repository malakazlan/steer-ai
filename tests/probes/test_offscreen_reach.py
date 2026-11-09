"""The offscreen probe's fixture: a button below the fold, a scrolled list, a hidden panel."""

from __future__ import annotations

from steerai.probes.offscreen_reach import OPTIONS, PAGE, TARGETS


def test_the_fixture_holds_each_case() -> None:
    assert 'style="height:2500px"' in PAGE
    assert PAGE.count('role="option"') == OPTIONS
    assert "<div hidden>" in PAGE
    assert {case for case, _role, _name in TARGETS} == {
        "below_fold",
        "in_list",
        "hidden",
        "drawer",
        "sr_only",
        "inert",
        "closed_select",
        "closed_details",
    }
    assert "<select" in PAGE
    assert "<details>" in PAGE
