"""Chromium accessibility probe: pure helpers (test page, stabilisation, CDP framing)."""

import json

import pytest

from steerai.probes.chrome_a11y import (
    CdpMessage,
    build_test_page,
    first_stable_index,
    parse_cdp_message,
)


def test_test_page_has_requested_number_of_interactive_elements() -> None:
    html = build_test_page(links=120, buttons=30)
    assert html.count("<a ") == 120
    assert html.count("<button") == 30
    assert "<title>Steer a11y probe</title>" in html


def test_first_stable_index_finds_where_counts_stop_growing() -> None:
    # counts stabilise once three consecutive readings are equal
    assert first_stable_index([10, 250, 480, 498, 498, 498, 498], window=3) == 3


def test_first_stable_index_none_when_never_stable() -> None:
    assert first_stable_index([1, 2, 3, 4], window=3) is None


def test_first_stable_index_rejects_bad_window() -> None:
    with pytest.raises(ValueError, match="window must be >= 2"):
        first_stable_index([1, 1, 1], window=1)


def test_cdp_message_round_trip() -> None:
    message = CdpMessage(id=7, method="Input.dispatchMouseEvent", params={"type": "mousePressed"})
    raw = message.to_json()
    assert json.loads(raw) == {
        "id": 7,
        "method": "Input.dispatchMouseEvent",
        "params": {"type": "mousePressed"},
    }


def test_parse_cdp_response_and_event() -> None:
    response = parse_cdp_message('{"id": 7, "result": {"ok": true}}')
    assert response.id == 7
    assert response.result == {"ok": True}
    assert response.method is None
    event = parse_cdp_message('{"method": "Page.loadEventFired", "params": {"timestamp": 1}}')
    assert event.id is None
    assert event.method == "Page.loadEventFired"
