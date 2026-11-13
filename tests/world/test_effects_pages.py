"""The effect observer on real Chrome pairs (packet v2-step2-effect-observer v3, invariant 3;
[V2.3]): recorded by `steerai.probes.effect_pages --section pairs` from local pages in a
throwaway profile, through DevTools with no OS input. Each pair is one rule; the browser frame
(the tab name's "Memory usage", the Back button) changes on its own and is `frame_changed`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from steerai.world.effects import EffectReport, Observed, observe
from steerai.world.model import snapshot_from_json

FIXTURES = Path(__file__).parent / "fixtures" / "effects"


def _report(case: str) -> EffectReport:
    data = json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))
    before = Observed(
        snapshot=snapshot_from_json(json.dumps(data["before"])),
        url=data["before_url"],
        windows=tuple(data["windows_before"]),
    )
    after = Observed(
        snapshot=snapshot_from_json(json.dumps(data["after"])),
        url=data["after_url"],
        windows=tuple(data["windows_after"]),
    )
    acted = tuple(data["acted"]) if data["acted"] else None
    return observe(before, after, acted=acted, text=data["text"])


@pytest.mark.parametrize(
    ("case", "classes", "carriers"),
    [
        ("tree_expand", {"own_state", "revealed"}, set()),
        ("menu_under_button", {"own_state", "revealed", "content_changed"}, set()),
        ("instant_results", {"own_state", "revealed", "content_changed"}, {"results"}),
        ("dropdown_close", {"own_state"}, set()),
        ("results_page", {"navigated"}, {"url_param", "title", "results"}),
        ("push_state", {"url_changed_in_place"}, {"url_param"}),
        ("enter_nothing", set(), set()),
        ("rerender", set(), set()),
    ],
)
def test_each_real_pair_gives_its_rule(case: str, classes: set[str], carriers: set[str]) -> None:
    report = _report(case)
    assert report.classes == classes
    assert report.carriers == carriers
    assert report.unread == frozenset()


def test_the_browser_frame_is_never_content() -> None:
    for case in ("push_state", "enter_nothing", "rerender", "tree_expand"):
        report = _report(case)
        assert "frame_changed" in report.tokens, case
        assert "none" not in report.classes, case


def test_a_closing_dropdown_and_a_rerender_are_their_tokens() -> None:
    assert "dismissed" in _report("dropdown_close").tokens
    assert "rerendered" in _report("rerender").tokens
