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
            "4531e697253a877ef7b8f3145ae77c61092465ce3be6f8c83c0b1eb9d75c5603",
            "54a6ebf9a8b041849256e594809091837ed92a160627de58800c12aae4f36dd8",
        ],
    ),
    "decide_later_turn": (
        lambda: c.build_prompt("g", ["s"], [], [], "t", [], task_fence=FENCE, screen_fence=SCREEN),
        [
            "4531e697253a877ef7b8f3145ae77c61092465ce3be6f8c83c0b1eb9d75c5603",
            "c690da9591b6a17390c491bd30f3ef9547b56c7b431e11d8e5d3b5948f68c5a7",
        ],
    ),
    "intake": (
        lambda: c.build_intake_prompt("g", [], task_fence=FENCE),
        [
            "9fa9b4d4215aaa2b33ddc5bb278ef6c2b596fd65d8636fbddae5511c7d127c74",
            "d5a3af9cb3f90d3f9fbb3155c26dcbd4c9cfb22a5e9018c773f4cc6a9ae7d21a",
        ],
    ),
    "audit": (
        lambda: c.build_audit_prompt("g", [], "t", [], task_fence=FENCE, screen_fence=SCREEN),
        [
            "4a7196a4ae75bb64eadc5aec748de109d71168149194b259c330db982f53cbc4",
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
