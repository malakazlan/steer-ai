"""A search field that swaps its role after typing (packet wiki-field-swap-2026-11-11; #144,
#145). Measured 2026-11-11 on Wikipedia (n=12, real keystrokes): the `Edit` becomes a
`ComboBox` of the same name 0.87-0.99 s after the `type` step returned (8/12), during the typing
(4/12), and once the reveal went straight to the ComboBox. The 1.0 s quiet window of [v2.44]
closed just before the swap (`screen_changed_during_ask`, `composer_unreadable`); the type's
readback found no `Edit` (`target_not_found`); the submit written for the Edit found no match."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.loop import ALIAS_ROLES, QUIET_AFTER_TYPE_S, SETTLE_AFTER_TYPE_CAP_S
from steerai.verify.predicates import FIELD_ROLES
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_loop import HELLO, Harness, _type
from tests.orchestrator.test_settle_after_type import PARAMS, _harness, _rerendering_page

SWAPPED = (COMPOSER[0], "ComboBox", COMPOSER[2], (42, 141), COMPOSER[4])
SUGGESTION = (30, "ListItem", "hello there wiki", (42, 140), COMPOSER[4])


def _submit() -> Step:
    return Step(
        id="s3",
        subgoal="g1",
        verb="submit",
        target=Target(role="Edit", name="Message"),
        risk="R2",
        params=PARAMS,
    )


def _swapping_page(h: Harness, *, after_s: float | None, during_type: bool = False) -> None:
    """The composer becomes a same-named ComboBox (a new RuntimeId) `after_s` after typing, or
    at the typing itself; a suggestion row comes with it."""
    typed: dict[str, float] = {}
    inner = h.world.observe

    def swap() -> None:
        if COMPOSER in h.world.rows:
            h.world.rows = [SWAPPED if r == COMPOSER else r for r in h.world.rows]
            h.world.rows.append(SUGGESTION)

    class Page:
        def observe(self, hwnd: int, **kwargs: Any) -> Any:
            if after_s is not None and "t" in typed and h.clock["t"] - typed["t"] >= after_s:
                swap()
            return inner(hwnd, **kwargs)

    h.session._observer = Page()

    def on_execute(_executor: Any, action: Any) -> None:
        if action.verb == "type":
            h.world.composer_value = action.text
            typed["t"] = h.clock["t"]
            if during_type:
                swap()
        if action.verb == "press" and action.chord == "enter":
            h.world.sent.append(h.world.composer_value)
            h.world.add_bubble(h.world.composer_value)
            h.world.composer_value = ""

    h.physical.on_execute = on_execute
    h.confirmer.on_ask = lambda _ask: h.clock.__setitem__("t", h.clock["t"] + 2.0)


# ---- R1: the quiet window covers the measured swap --------------------------------------------


def test_the_quiet_window_and_its_cap_are_the_measured_numbers() -> None:
    assert QUIET_AFTER_TYPE_S == 2.0, "max swap 0.99 s after the type step (n=5), doubled"
    assert SETTLE_AFTER_TYPE_CAP_S == 5.0, "the cap keeps 3.0 s of reach past the window"


def test_a_page_that_re_renders_1_3_s_after_typing_is_still_waited_for() -> None:
    """The [v2.44] fixture at Wikipedia's measured timing: the swap lands after the old 1.0 s
    window closed. The before of record must be taken after it."""
    h = _harness()
    _rerendering_page(h, after_s=1.3)
    assert h.session.run_step(_type()).ok is True
    result = h.session.run_step(_submit())
    assert result.evidence.get("detail") != "screen_changed_during_ask", result.to_dict()
    assert result.ok is True, result.to_dict()
    assert len(h.confirmer.asked) == 1


# ---- R2: a same-named field that swapped between Edit and ComboBox is the same field ----------


def test_the_alias_set_is_every_field_role_on_both_sides() -> None:
    from steerai.verify.t1 import ALIAS_FIELD_ROLES  # noqa: PLC0415 - the condition side

    assert ALIAS_ROLES == FIELD_ROLES
    assert ALIAS_FIELD_ROLES == FIELD_ROLES


def test_a_type_whose_field_became_a_combobox_while_typing_is_verified_through_it() -> None:
    """4/12 live: the readback looked for the Edit and found none (`target_not_found`)."""
    h = _harness()
    _swapping_page(h, after_s=None, during_type=True)
    result = h.session.run_step(_type())
    assert result.ok is True, result.to_dict()
    assert result.evidence.get("alias") == "ComboBox"


def test_a_submit_on_the_edit_resolves_the_same_named_combobox_and_reads_it() -> None:
    """The submit was planned for the Edit; the field is a ComboBox by the time it runs. The
    composer check reads the ComboBox, the ask names the role the person will see."""
    h = _harness()
    _swapping_page(h, after_s=0.3)
    assert h.session.run_step(_type()).ok is True
    result = h.session.run_step(_submit())
    assert result.ok is True, result.to_dict()
    assert result.evidence.get("alias") == "ComboBox"
    assert h.world.sent == [HELLO]
    assert len(h.confirmer.asked) == 1
    assert h.confirmer.asked[0].target_role == "ComboBox"


def test_two_same_named_comboboxes_are_ambiguous_for_the_swapped_submit() -> None:
    h = _harness()
    _swapping_page(h, after_s=0.3)
    assert h.session.run_step(_type()).ok is True
    h.world.rows.append((31, "ComboBox", COMPOSER[2], (42, 142), COMPOSER[4]))
    result = h.session.run_step(_submit())
    assert result.ok is False
    assert result.evidence.get("detail") == "ambiguous_target", result.to_dict()
    assert h.world.sent == []


def test_a_choose_never_takes_the_alias() -> None:
    h = _harness()
    h.world.rows = [SWAPPED if r == COMPOSER else r for r in h.world.rows]
    step = Step(
        id="s9",
        subgoal="g1",
        verb="choose",
        target=Target(role="ComboBox", name="Options"),
        risk="R1",
        params={"option": "Blue"},
    )
    h.world.rows = [r for r in h.world.rows if r[0] != COMPOSER[0]]
    h.world.rows.append((COMPOSER[0], "Edit", "Options", (42, 141), COMPOSER[4]))
    result = h.session.run_step(step)
    assert result.ok is False
    assert result.evidence.get("alias") is None


# ---- hunter rows FS-01, FS-04, FS-10, FS-17, FS-18, FS-29 and the focus readback ----------------


def test_a_role_flip_on_the_same_runtime_id_restarts_the_quiet_window() -> None:
    """FS-01: an ARIA widget that sets role=combobox on the same input keeps its RuntimeId;
    the quiet set is (RuntimeId, role), so the flip counts as a change."""
    h = _harness()
    typed: dict[str, float] = {}
    inner = h.world.observe
    flipped = (COMPOSER[0], "ComboBox", COMPOSER[2], COMPOSER[3], COMPOSER[4])

    class Page:
        def observe(self, hwnd: int, **kwargs: Any) -> Any:
            if "t" in typed and h.clock["t"] - typed["t"] >= 1.5 and COMPOSER in h.world.rows:
                h.world.rows = [flipped if r == COMPOSER else r for r in h.world.rows]
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
    assert h.session.run_step(_type()).ok is True
    before = h.clock["t"]
    result = h.session.run_step(_submit())
    assert result.evidence.get("detail") != "screen_changed_during_ask", result.to_dict()
    assert result.evidence.get("alias") == "ComboBox"
    assert len(h.confirmer.asked) == 1
    assert h.confirmer.asked[0].target_role == "ComboBox", "the ask came after the flip"
    waited = h.clock["t"] - before - 2.0
    assert waited >= 1.5 + QUIET_AFTER_TYPE_S - 0.5, "the window restarted at the flip"


def test_two_late_changes_inside_the_cap_still_settle_never_capped() -> None:
    """FS-04: changes 1.2 s and 2.3 s after typing; a full quiet window fits before the cap."""
    h = _harness()
    typed: dict[str, float] = {}
    inner = h.world.observe
    refreshed = (SUGGESTION[0], SUGGESTION[1], SUGGESTION[2], (42, 143), SUGGESTION[4])

    class Page:
        def observe(self, hwnd: int, **kwargs: Any) -> Any:
            if "t" in typed:
                since = h.clock["t"] - typed["t"]
                if since >= 1.2 and COMPOSER in h.world.rows:
                    h.world.rows = [SWAPPED if r == COMPOSER else r for r in h.world.rows]
                    h.world.rows.append(SUGGESTION)
                if since >= 2.3 and SUGGESTION in h.world.rows:
                    # the suggestion list re-renders once more (a second fetch)
                    h.world.rows = [refreshed if r == SUGGESTION else r for r in h.world.rows]
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
    assert h.session.run_step(_type()).ok is True
    result = h.session.run_step(_submit())
    assert result.ok is True, result.to_dict()
    assert "settle" not in result.evidence, "never capped"


def test_a_field_that_was_secure_never_aliases_to_a_plain_one() -> None:
    """FS-10: the task's last look showed a password field of that name; a same-named plain
    ComboBox shown instead never receives the text."""
    from dataclasses import replace  # noqa: PLC0415

    from steerai.orchestrator.loop import _StepFailureError  # noqa: PLC0415

    h = _harness()
    shown = h.world.observe(0x10)
    secure = [replace(e, secure=True) if e.id == COMPOSER[0] else e for e in shown.elements]
    h.session._last_after = (replace(shown, elements=secure), h.clock["t"])
    h.world.rows = [SWAPPED if r == COMPOSER else r for r in h.world.rows]
    with pytest.raises(_StepFailureError) as refused:
        h.session._resolve_alias(_type(), h.world.observe(0x10), {})
    assert (refused.value.code.value, refused.value.detail) == ("gate_blocked", "alias_secure")


def test_a_settled_page_skips_the_no_match_settle_before_the_alias() -> None:
    """FS-17 (live wk2 n=3: `settled: 2`, 1.0 s of policy time after the page was proven
    quiet): a before that settled after typing takes the alias at once."""
    h = _harness()
    _swapping_page(h, after_s=0.3)
    assert h.session.run_step(_type()).ok is True
    result = h.session.run_step(_submit())
    assert result.ok is True, result.to_dict()
    assert "settled" not in result.evidence


def test_the_scroll_quiet_look_keeps_its_own_window() -> None:
    """FS-18: the offscreen quiet look ([v2.55]) is not doubled by the typing window."""
    from steerai.orchestrator.loop import SCROLL_QUIET_S  # noqa: PLC0415

    assert SCROLL_QUIET_S == 1.0


def test_a_document_never_aliases_a_combobox_and_back() -> None:
    """FS-29: the pairs are explicit (Edit<->Document, Edit<->ComboBox); never by transitivity."""
    from steerai.verify.predicates import alias_roles  # noqa: PLC0415

    assert alias_roles("Edit") == frozenset({"Document", "ComboBox"})
    assert alias_roles("Document") == frozenset({"Edit"})
    assert alias_roles("ComboBox") == frozenset({"Edit"})
    assert alias_roles("Button") == frozenset()


def test_a_focus_whose_field_swapped_before_the_readback_is_verified_through_it() -> None:
    """Probe 2026-11-11 (2 of 7 runs): the Edit became the ComboBox between the click's reveal
    and the focus step's readback (`target_not_found`)."""
    from tests.orchestrator.test_settle_after_type import FollowingFocus  # noqa: PLC0415

    h = Harness()
    from tests.orchestrator.test_edge_cases import _rebuild  # noqa: PLC0415

    _rebuild(h, live=FollowingFocus(h.world))
    focus = Step(
        id="s1", subgoal="g1", verb="focus", target=Target(role="Edit", name="Message"), risk="R0"
    )

    def on_execute(_executor: Any, action: Any) -> None:
        # the focus lands (SetFocus is a semantic click on the field) and the page swaps
        h.world.rows = [SWAPPED if r == COMPOSER else r for r in h.world.rows]

    h.semantic.on_execute = on_execute
    h.physical.on_execute = on_execute
    result = h.session.run_step(focus)
    assert result.ok is True, result.to_dict()
    assert result.evidence.get("alias") == "ComboBox"
