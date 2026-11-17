"""#484: the fixed prefix of every judge call is cacheable, and its text is frozen.

The sha256 pins were taken on main before the cache marker was added: the marker changes the
request's shape, never a byte of the rendered rules or schema."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from types import SimpleNamespace

from steerai.loop2 import controller as c
from steerai.loop2.decide_port import anthropic_call, parse_usage
from steerai.loop2.loop import TokenUsage
from tests.loop2.test_decide_port import FakeMessages, _response

FENCE = "a" * 8
SCREEN = "b" * 8
Prompt = Callable[[], tuple[list[str], list[str]]]

PINS: dict[str, tuple[Prompt, list[str]]] = {
    "decide_first_turn": (
        lambda: c.build_prompt("g", [], [], [], "t", [], task_fence=FENCE, screen_fence=SCREEN),
        [
            "6768a0b976c1066a430749fafeb39966e45a3422563343723c699b470ef69c1a",
            "45d33f68f06d7673a75a167bcecf7edfff54253f234d286109bfa96267ae0345",
        ],
    ),
    "decide_later_turn": (
        lambda: c.build_prompt("g", ["s"], [], [], "t", [], task_fence=FENCE, screen_fence=SCREEN),
        [
            "6768a0b976c1066a430749fafeb39966e45a3422563343723c699b470ef69c1a",
            "c690da9591b6a17390c491bd30f3ef9547b56c7b431e11d8e5d3b5948f68c5a7",
        ],
    ),
    "intake": (  # re-pinned at intake-6: [V2.89]'s narrower-field line (the rules block only)
        lambda: c.build_intake_prompt("g", [], task_fence=FENCE),
        [
            "4a2e860de1b8625796c79d2aa2bbf241f0eb32f69c468571c640a9c520f279e9",
            "d5a3af9cb3f90d3f9fbb3155c26dcbd4c9cfb22a5e9018c773f4cc6a9ae7d21a",
        ],
    ),
    "audit": (
        lambda: c.build_audit_prompt("g", [], "t", [], task_fence=FENCE, screen_fence=SCREEN),
        [
            "44c6da8d264f101e33664c9b1af63dc053b920a970afb4d9885151168b952b31",
            "d5a3af9cb3f90d3f9fbb3155c26dcbd4c9cfb22a5e9018c773f4cc6a9ae7d21a",
        ],
    ),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_rendered_rules_and_schema_of_every_judge_are_byte_identical() -> None:
    for name, (build, pinned) in PINS.items():
        system, _ = build()
        assert [_sha(block) for block in system] == pinned, name


def test_the_cache_marker_sits_on_the_last_system_block_of_every_judge_and_nowhere_else() -> None:
    for name, (build, _) in PINS.items():
        system, user = build()
        messages = FakeMessages(_response())
        anthropic_call(SimpleNamespace(messages=messages), max_tokens=64)(system, user)
        sent = messages.kwargs["system"]
        assert isinstance(sent, list), name
        assert [b["text"] for b in sent] == system, name  # the text is sent as built
        assert [b.get("cache_control") for b in sent] == [None] * (len(sent) - 1) + [
            {"type": "ephemeral"}
        ], name
        for message in messages.kwargs["messages"]:  # type: ignore[attr-defined]  # kwargs is object
            assert all("cache_control" not in part for part in message["content"]), name
        assert "cache_control" not in messages.kwargs, name


def test_a_cache_hit_reads_as_cache_read_and_is_priced_at_the_read_rate() -> None:
    raw = {
        "input_tokens": 300,
        "output_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 2_700,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
    }
    usage = parse_usage(raw)
    assert usage == TokenUsage(input_tokens=300, output_tokens=50, cache_read=2_700)


def test_a_cache_write_is_split_by_lifetime_and_never_double_counted() -> None:
    raw = {
        "input_tokens": 300,
        "output_tokens": 50,
        "cache_creation_input_tokens": 2_700,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 2_700, "ephemeral_1h_input_tokens": 0},
    }
    assert parse_usage(raw) == TokenUsage(input_tokens=300, output_tokens=50, cache_write_5m=2_700)
