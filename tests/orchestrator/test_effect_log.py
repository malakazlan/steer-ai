"""The effect report logged beside v1 (V2 step 2a, [V2.2]; packet v3; hunter EO-33/42): one
`effect` record per verified step, closed tokens only, computed from values v1 already read.
Nothing reads it back; a failing observer is one `build_failed` token and the step is the
same."""

from __future__ import annotations

import json
from typing import Any

import pytest

import steerai.orchestrator.loop as loop_mod
from tests.orchestrator.test_loop import HELLO, Harness, _send, _type
from tests.orchestrator.test_url_query_loop import HOME, RESULTS, Bar, _harness, _submit


def _effects(h: Harness) -> list[dict[str, Any]]:
    return [r for r in map(json.loads, h.trace.lines) if r.get("kind") == "effect"]


def test_each_verified_step_logs_one_effect_record_of_closed_tokens() -> None:
    h = Harness()
    h.session.run_step(_type())
    h.session.run_step(_send())
    effects = _effects(h)
    assert [e["step_id"] for e in effects] == ["s2", "s3"]
    for effect in effects:
        assert set(effect) == {
            "kind",
            "task_id",
            "step_id",
            "before",
            "primary",
            "classes",
            "tokens",
            "unread",
            "carriers",
            "revealed",
            "stable_after",
        }
        assert "windows" in effect["unread"], "no window list after in 2a: never `none`"
        assert "none" not in effect["classes"]
    clear = "\n".join(h.trace.lines)
    for sentinel in (HELLO, "Message", "Amir"):
        assert sentinel not in clear


def test_an_address_bar_proof_feeds_the_url_and_its_carrier() -> None:
    h = _harness(Bar(HOME, RESULTS))
    h.session.run_step(_submit())
    (effect,) = _effects(h)
    assert "url" not in effect["unread"]
    assert "url_param" in effect["carriers"]
    assert "bing.com" not in "\n".join(h.trace.lines)


def test_an_observer_that_fails_is_one_token_and_the_step_is_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = Harness().session.run_step(_type()).to_dict()

    def broken(*_a: Any, **_k: Any) -> Any:
        raise ValueError(f"no such element {HELLO}")

    monkeypatch.setattr(loop_mod, "observe", broken)
    h = Harness()
    result = h.session.run_step(_type())
    assert result.to_dict() == plain
    (effect,) = _effects(h)
    assert effect == {
        "kind": "effect",
        "task_id": "task-1",
        "step_id": "s2",
        "detail": "build_failed",
    }
    assert HELLO not in "\n".join(h.trace.lines)
