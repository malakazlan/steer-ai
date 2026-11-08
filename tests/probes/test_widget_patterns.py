"""The widget probe's fixture: one of each widget kind, each logging its events."""

from __future__ import annotations

from steerai.probes.widget_patterns import PAGE, PATTERNS


def test_the_fixture_holds_each_widget_and_logs_its_events() -> None:
    for marker in ('type="checkbox"', "<select", 'role="tab"', 'type="radio"', "window.LOG"):
        assert marker in PAGE
    assert PATTERNS["Toggle"] == 10015
    assert PATTERNS["SelectionItem"] == 10010
