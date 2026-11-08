"""An irreversible step right after typing waits for the page to go quiet (5.11 [v2.44]): live
2026-11-08, Wikipedia's suggestion dropdown re-rendered the search widget 0.39-0.89 s after the
text landed (n=5), after the R2 snapshot was taken, so the step stopped
`screen_changed_during_ask` (2/10) or read a stale composer `composer_unreadable` (3/10)."""

from __future__ import annotations

from typing import Any

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.loop import QUIET_AFTER_TYPE_S
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_edge_cases import FocusLive, _rebuild
from tests.orchestrator.test_loop import HELLO, Harness, _send, _type

PARAMS = {
    "text": HELLO,
    "composer_role": "Edit",
    "composer_name": "Message",
    "scope": "Conversation",
}


def _submit() -> Step:
    """Enter in the search field itself, as on Wikipedia: the asked element is the field."""
    return Step(
        id="s3",
        subgoal="g1",
        verb="submit",
        target=Target(role="Edit", name="Message"),
        risk="R2",
        params=PARAMS,
    )


class FollowingFocus(FocusLive):
    """Keyboard focus stays in the composer, whatever RuntimeId it has now."""

    def focused_runtime_id(self) -> tuple[int, ...]:
        return next(r[3] for r in self.world.rows if r[0] == COMPOSER[0])


def _harness() -> Harness:
    h = Harness()
    _rebuild(h, live=FollowingFocus(h.world))
    return h


RERENDERED = (COMPOSER[0], COMPOSER[1], COMPOSER[2], (42, 141), COMPOSER[4])
SUGGESTION = (30, "ListItem", "hello there wiki", (42, 140), COMPOSER[4])


def _rerendering_page(h: Harness, after_s: float = 0.4) -> dict[str, float]:
    """0.4 s after typing the page re-renders the composer (a new RuntimeId) and adds a
    suggestion row; a person takes 2 s to answer an ask."""
    typed: dict[str, float] = {}
    inner = h.world.observe

    class Page:
        def observe(self, hwnd: int, **kwargs: Any) -> Any:
            if "t" in typed and h.clock["t"] - typed["t"] >= after_s and COMPOSER in h.world.rows:
                h.world.rows = [RERENDERED if r == COMPOSER else r for r in h.world.rows]
                h.world.rows.append(SUGGESTION)
            return inner(hwnd, **kwargs)

    h.session._observer = Page()

    def on_execute(_executor: Any, action: Any) -> None:
        if action.verb == "type":
            h.world.composer_value = action.text
            typed["t"] = h.clock["t"]
        if action.verb == "press" and action.chord == "enter":
            h.world.sent.append(h.world.composer_value)
            h.world.add_bubble(h.world.composer_value)
            h.world.composer_value = ""

    h.physical.on_execute = on_execute
    h.confirmer.on_ask = lambda _ask: h.clock.__setitem__("t", h.clock["t"] + 2.0)
    return typed


def test_a_submit_right_after_typing_waits_for_the_page_to_go_quiet() -> None:
    h = _harness()
    _rerendering_page(h)
    assert h.session.run_step(_type()).ok is True
    result = h.session.run_step(_submit())
    assert result.evidence.get("detail") != "screen_changed_during_ask", result.to_dict()
    assert result.ok is True, result.to_dict()
    assert result.evidence["settled_after_type"] is True
    assert len(h.confirmer.asked) == 1


def test_the_settle_waits_a_quiet_window_and_no_longer() -> None:
    h = _harness()
    _rerendering_page(h)
    h.session.run_step(_type())
    before = h.clock["t"]
    h.session.run_step(_submit())
    waited = h.clock["t"] - before - 2.0  # the person's 2 s answer is not the settle
    assert QUIET_AFTER_TYPE_S <= waited <= QUIET_AFTER_TYPE_S + 1.0


def test_a_step_not_right_after_a_type_does_not_wait() -> None:
    h = Harness()
    h.world.composer_value = "hello there"
    h.confirmer.on_ask = lambda _ask: None
    before = h.clock["t"]
    result = h.session.run_step(_send())
    assert result.ok is True, result.to_dict()
    assert "settled_after_type" not in result.evidence
    assert h.clock["t"] - before < QUIET_AFTER_TYPE_S
