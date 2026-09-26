"""Jev client: element Choice over an element list, parsed into a typed answer with margin."""

import json

import httpx2
import pytest

from steerai.jev import ElementChoice, JevClient, build_element_question

LINES = [
    '1 Window "Inbox" [0,0,800,600]',
    '2   ListItem "Amir · hi bro · 2h" [20,140,300,190]',
    '3   ListItem "Amir Khan · ok · 3d" [20,190,300,240]',
    '4   Button "New message" [320,60,420,80]',
]


def test_build_element_question_has_one_option_per_line_plus_none() -> None:
    state, question = build_element_question(intent="open the chat with Amir Khan", lines=LINES)
    assert state["intent"] == "open the chat with Amir Khan"
    assert state["screen"] == "\n".join(LINES)
    assert question.type == "choice"
    assert set(question.criteria) == {"1", "2", "3", "4", "none"}
    assert question.criteria["3"] == LINES[2]


def test_choose_element_parses_choice_probabilities_and_margin() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "target": {
                        "type": "choice",
                        "choice": "3",
                        "probabilities": {"1": 0.01, "2": 0.14, "3": 0.8, "4": 0.03, "none": 0.02},
                        "confidence": 0.75,
                    }
                },
                "usage": {"input_tokens": 120, "output_tokens": 8},
            },
        )

    client = JevClient(api_key="test-key", transport=httpx2.MockTransport(handler))
    answer = client.choose_element(intent="open the chat with Amir Khan", lines=LINES)

    assert isinstance(answer, ElementChoice)
    assert answer.element_id == 3
    assert answer.confidence == 0.75
    assert answer.margin == pytest.approx(0.8 - 0.14)
    assert answer.model == "jev-1.13.0"
    assert answer.latency_ms >= 0
    assert seen["path"] == "/v1/systemone"
    assert seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["questions"]["target"]["type"] == "choice"


def test_base_url_routes_to_a_gateway_with_the_same_path() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["host"] = request.url.host
        seen["path"] = request.url.path
        return httpx2.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "target": {
                        "type": "choice",
                        "choice": "4",
                        "probabilities": {"1": 0.0, "2": 0.0, "3": 0.0, "4": 1.0, "none": 0.0},
                        "confidence": 1.0,
                    }
                },
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = JevClient(
        api_key="or-key",
        base_url="https://openrouter.ai/api",
        transport=httpx2.MockTransport(handler),
    )
    assert client.choose_element(intent="new message", lines=LINES).element_id == 4
    assert seen == {"host": "openrouter.ai", "path": "/api/v1/systemone"}


def test_client_from_env_prefers_explicit_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("STEER_JEV_BASE_URL", "https://openrouter.ai/api")
    monkeypatch.setenv("STEER_JEV_MODEL", "typesafe/jev-1.13")
    settings = JevClient.settings_from_env()
    assert (settings.api_key, settings.base_url, settings.model) == (
        "k",
        "https://openrouter.ai/api",
        "typesafe/jev-1.13",
    )
    monkeypatch.delenv("STEER_JEV_BASE_URL")
    monkeypatch.delenv("STEER_JEV_MODEL")
    settings = JevClient.settings_from_env()
    assert (settings.base_url, settings.model) == (None, "jev-latest")
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(OSError, match="TYPESAFE_API_KEY not set"):  # message prefix is stable
        JevClient.from_env()


def test_choose_element_none_maps_to_no_element() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "target": {
                        "type": "choice",
                        "choice": "none",
                        "probabilities": {"1": 0.1, "2": 0.1, "3": 0.1, "4": 0.1, "none": 0.6},
                        "confidence": 0.5,
                    }
                },
                "usage": {"input_tokens": 100, "output_tokens": 8},
            },
        )

    client = JevClient(api_key="test-key", transport=httpx2.MockTransport(handler))
    answer = client.choose_element(intent="pay the invoice", lines=LINES)
    assert answer.element_id is None
    assert answer.margin == pytest.approx(0.5)
