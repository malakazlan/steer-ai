"""The effect report logged beside v1 (V2 step 2a, [V2.2]; packet v3; hunter EO-33/42): one
`effect` record per verified step, closed tokens only, computed from values v1 already read.
Nothing reads it back; a failing observer is one `build_failed` token and the step is the
same."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

import steerai.orchestrator.loop as loop_mod
from steerai.world.effects import NOT_OBSERVED
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
            "not_observed",
        }
        assert "windows" in effect["unread"], "no window list read at all: never `none`"
        assert effect["not_observed"] == list(NOT_OBSERVED)
        assert "url" not in effect["unread"], "no URL read yet in this task: not a fact"
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


def test_a_step_never_logs_the_previous_steps_acted_element() -> None:
    """#262 review: `_begin_step` resets the acted element; `_record` sets it again."""
    h = Harness()
    h.session.run_step(_type())
    h.session._acted_rid = (99, 99)
    h.session._begin_step(_send(), None)
    assert not h.session._acted_rid


@dataclass(frozen=True)
class _Window:
    hwnd: int
    title: str = "Chat"
    pid: int = 7
    cloak_unreadable: bool = False


def test_the_window_list_verify_reads_is_the_after_list() -> None:
    """[V2.3]: v1's verify already reads the window list after the act; the report uses it, so
    `windows` is unread only when no list was read."""
    h = Harness()
    h.session._windows = lambda: [_Window(hwnd=0x10)]
    h.session.run_step(_type())
    (effect,) = _effects(h)
    assert "windows" not in effect["unread"]
    assert "windows_changed" not in effect["tokens"]


def test_a_key_press_keeps_the_last_targeted_element_as_its_scope() -> None:
    """[V2.3]: a step with no target takes the scope of the last targeted element; a new step
    resets the acted element but never the scope."""
    h = Harness()
    h.session.run_step(_type())
    targeted = h.session._acted_rid
    assert targeted
    h.session._begin_step(_send(), None)
    assert not h.session._acted_rid
    assert h.session._scope_rid == targeted


def test_a_key_press_after_the_page_changed_has_no_inherited_scope() -> None:
    """#263 review: the last target scopes a key press only while the top Document it was taken
    on is still the top Document; after a navigation the whole window is compared."""
    h = Harness()
    h.session.run_step(_type())
    before = h.world.observe(0x10)
    assert h.session._key_scope(before) == h.session._scope_rid
    h.session._scope_doc = (77, 77)  # the page it was taken on is gone
    assert h.session._key_scope(before) is None


def test_after_a_url_was_read_a_later_step_without_one_has_it_unread() -> None:
    h = _harness(Bar(HOME, RESULTS))
    h.session.run_step(_submit())
    assert h.session._url_fact


@pytest.mark.parametrize(
    ("url", "parsable"),
    [
        ("https://shop.test/a", True),
        ("file:///C:/pages/a.html", True),
        ("www.bing.com/", True),
        ("chrome://newtab/", False),
        ("", False),
        ("search terms", False),
    ],
)
def test_a_parsable_url_has_a_web_scheme_or_a_dotted_host(url: str, parsable: bool) -> None:
    assert loop_mod._parsable_url(url) is parsable
