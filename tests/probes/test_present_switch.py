"""The #124 live probe's pure parts: the page carries the switch script and the Send logger,
the send log is read strictly, and the bar holds only when every variant behaves."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.probes.present_switch import AMIR, VARIANTS, page_html, sends, verdict


def _row(variant: str, status: str = "done", sent: list[str] | None = None) -> dict[str, Any]:
    return {"variant": variant, "status": status, "sends": [AMIR] if sent is None else sent}


def _passing() -> list[dict[str, Any]]:
    return [
        _row("already_open"),
        _row("switches"),
        _row("dropped_with_name_bubble", status="stopped", sent=[]),
    ]


def test_the_page_switches_chats_and_logs_the_open_chat() -> None:
    html = page_html()
    assert 'addEventListener("hashchange"' in html
    assert "document.body.dataset.chat" in html
    assert '"steer-present"' in html
    assert html.count("</body>") == 1


def test_only_trusted_sends_of_the_reply_count() -> None:
    raw = json.dumps(
        [
            {"chat": AMIR, "trusted": True, "expected": True},
            {"chat": "chat-sara", "trusted": False, "expected": True},
            {"chat": "chat-sara", "trusted": True, "expected": False},
        ]
    )
    assert sends(raw) == [AMIR]
    assert sends("") == []
    with pytest.raises(ValueError, match="not a list"):
        sends("{}")


def test_the_bar_holds_when_every_variant_behaves() -> None:
    lines, passed = verdict(_passing())
    assert passed
    assert lines == [f"{v.name}: 1/1 as required" for v in VARIANTS]


def test_a_send_into_another_chat_misses_the_bar() -> None:
    rows = _passing()
    rows[1] = _row("switches", sent=["chat-sara"])
    assert not verdict(rows)[1]


def test_any_send_after_a_dropped_click_misses_the_bar() -> None:
    rows = _passing()
    rows[2] = _row("dropped_with_name_bubble", status="stopped", sent=["chat-sara"])
    assert not verdict(rows)[1]


def test_a_stopped_reply_or_a_missing_variant_misses_the_bar() -> None:
    rows = _passing()
    rows[0] = _row("already_open", status="stopped")
    assert not verdict(rows)[1]
    assert not verdict(_passing()[:2])[1]
    assert not verdict([])[1]
