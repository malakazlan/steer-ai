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
            "d3b04209ffc57da87d088347b475757d16d7445cc0ce5689b16a3fb6d73e6de5",
            "89867d6c0d0313ac22bd5710ed660628f96cedd64a74b16fb2298639085dd7c0",
        ],
    ),
    "decide_later_turn": (
        lambda: c.build_prompt("g", ["s"], [], [], "t", [], task_fence=FENCE, screen_fence=SCREEN),
        [
            "d3b04209ffc57da87d088347b475757d16d7445cc0ce5689b16a3fb6d73e6de5",
            "fd96e791e7eb6fe8127261045310cf223ff7ecb2f8fe3da655fb05b55e608523",
        ],
    ),
    "intake": (
        lambda: c.build_intake_prompt("g", [], task_fence=FENCE),
        [
            "c5c7f2a04991b863cbbd7e666d0a96ea974cd15b4720d120a28b5a09a2181ef3",
            "d5a3af9cb3f90d3f9fbb3155c26dcbd4c9cfb22a5e9018c773f4cc6a9ae7d21a",
        ],
    ),
    "audit": (
        lambda: c.build_audit_prompt("g", [], "t", [], task_fence=FENCE, screen_fence=SCREEN),
        [
            "cfe44de6b8ac153f877bb22363154cc2e245ca0f379bc4f666a194cb831575b9",
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
