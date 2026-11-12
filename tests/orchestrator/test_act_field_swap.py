"""A field swapped during the act ([v2.82]; dev wk2 2026-11-12: the click revealed `Edit "Search
Wikipedia"`, which became a same-named ComboBox with a new RuntimeId while the focus acted, so
the executor found the Edit gone, the one retry's re-resolve found the ComboBox and the step
stopped `screen_changed_during_act`). [v2.71] FS-02 already follows that swap in the readback;
the in-act retry at R0/R1 for `focus` / `type` now follows it the same way: the one same-named,
non-secure, NEW field of the alias pair. At R2/R3 the asked identity stays RuntimeId and role
(#89, R3 of [v2.71])."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from tests.orchestrator.fakes import FakeExecutor, Row, not_delivered
from tests.orchestrator.test_edge_cases import FocusLive, _rebuild
from tests.orchestrator.test_loop import Harness

EDIT: Row = (50, "Edit", "Search Wikipedia", (42, 100), None)
COMBO: Row = (51, "ComboBox", "Search Wikipedia", (42, 101), None)


def _focus(risk: str = "R0") -> Step:
    return Step(
        id="f1",
        subgoal="g1",
        verb="focus",
        target=Target(role="Edit", name="Search Wikipedia"),
        risk=risk,  # type: ignore[arg-type]  # a Risk literal from the parameter
    )


def _harness(*swapped_in: Row, keep: bool = False) -> tuple[Harness, FocusLive]:
    """The Edit is on screen; the first act finds it gone (or, with `keep`, still there) and
    `swapped_in` beside it."""
    h = Harness()
    h.world.rows.append(EDIT)
    live = FocusLive(h.world)
    _rebuild(h, live=live)

    def swaps(executor: FakeExecutor, action: Any) -> None:
        if action.element_id == EDIT[0]:
            executor.outcomes[EDIT[0]] = not_delivered(ErrorCode.ELEMENT_NOT_FOUND, "element_gone")
            if not keep:
                h.world.rows.remove(EDIT)
            h.world.rows.extend(swapped_in)
            return
        live.focused = next(r[3] for r in h.world.rows if r[0] == action.element_id)

    h.physical.on_execute = swaps
    return h, live


def test_a_focus_whose_field_swapped_mid_act_focuses_the_new_field() -> None:
    h, live = _harness(COMBO)
    result = h.session.run_step(_focus())
    assert result.ok is True, result.to_dict()
    assert h.physical.calls == [("click", EDIT[0], None), ("click", COMBO[0], None)]
    assert live.focused == COMBO[3]
    assert result.evidence["act_alias"] == "ComboBox"
    assert result.evidence["retries"] == 1


@pytest.mark.parametrize(
    "swapped_in",
    [
        (),  # gone, nothing in its place
        (COMBO, (52, "ComboBox", "Search Wikipedia", (42, 102), None)),  # two: ambiguous
        ((51, "ComboBox", "Search Wiktionary", (42, 101), None),),  # another name
        ((51, "Button", "Search Wikipedia", (42, 101), None),),  # not a field of the pair
        ((51, "Text", "Search Wikipedia", (42, 101), None),),  # text, not a field
    ],
)
def test_no_single_swapped_field_is_a_changed_screen(swapped_in: tuple[Row, ...]) -> None:
    h, _live = _harness(*swapped_in)
    result = h.session.run_step(_focus())
    assert result.ok is False
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "screen_changed_during_act"
    assert h.physical.calls == [("click", EDIT[0], None)]


def test_a_field_that_was_already_on_screen_is_not_the_swap() -> None:
    """Provably new only: a ComboBox of that name present before the act is another field."""
    h, _live = _harness()
    h.world.rows.append(COMBO)
    result = h.session.run_step(_focus())
    assert result.ok is False
    assert result.evidence["detail"] == "screen_changed_during_act"
    assert h.physical.calls == [("click", EDIT[0], None)]


def test_a_secure_field_is_never_the_swap() -> None:
    h, _live = _harness(COMBO)
    h.world.secure_ids = {COMBO[0]}
    result = h.session.run_step(_focus())
    assert result.ok is False
    assert result.evidence["detail"] == "screen_changed_during_act"
    assert h.physical.calls == [("click", EDIT[0], None)]


def test_a_type_whose_field_swapped_before_any_key_types_into_the_new_field() -> None:
    h, _live = _harness(COMBO)
    h.world.composer_value = ""
    step = Step(
        id="t1",
        subgoal="g1",
        verb="type",
        target=Target(role="Edit", name="Search Wikipedia"),
        text="Ada Lovelace",
        risk="R1",
    )
    result = h.session.run_step(step)
    assert [c[1] for c in h.physical.calls] == [EDIT[0], COMBO[0]]
    assert result.evidence.get("act_alias") == "ComboBox", result.to_dict()
    assert result.ok is True, result.to_dict()
    assert h.world.composer_value == "Ada Lovelace"


@pytest.mark.parametrize("risk", ["R2", "R3"])
def test_the_retry_never_follows_a_swap_at_r2_or_r3(risk: str) -> None:
    """[v2.71] R3: at R2/R3 the asked identity is RuntimeId and role. No focus or type acts
    above R1 today (the gate refuses them `weak_predicate`), so the retry is pinned directly:
    a later tier rule must not open the alias to an approved step."""
    h, _live = _harness()
    previous = h.world.observe(0x10)
    asked = previous.element(EDIT[0])
    h.world.rows.remove(EDIT)
    h.world.rows.append(COMBO)
    now = h.world.observe(0x10)
    session: Any = h.session
    session._act_evidence = {}
    with pytest.raises(Exception, match="screen_changed_during_act"):
        session._retry_target(
            _focus(), risk, gate="approve", previous=previous, snapshot=now, asked=asked
        )
    picked = session._retry_target(
        _focus(), "R0", gate="approve", previous=previous, snapshot=now, asked=asked
    )
    assert picked == COMBO[0]


def test_a_new_twin_beside_the_asked_field_is_never_the_swap() -> None:
    """#236 review item 2: the asked Edit still there and a new same-named Edit beside it is an
    ambiguity, never a swap; only a gone field is followed."""
    h, _live = _harness((52, "Edit", "Search Wikipedia", (42, 102), None), keep=True)
    result = h.session.run_step(_focus())
    assert result.ok is False
    assert result.evidence["detail"] == "screen_changed_during_act"
    assert h.physical.calls == [("click", EDIT[0], None)]


def test_a_type_the_person_said_yes_to_never_follows_the_swap() -> None:
    """#236 review item 1: generated text asks at R1; the yes covered the Edit the person saw
    ([v2.71] R3), so a swap under the act stays a changed screen."""
    h, _live = _harness(COMBO)
    step = Step(
        id="t1",
        subgoal="g1",
        verb="type",
        target=Target(role="Edit", name="Search Wikipedia"),
        text="Ada Lovelace",
        text_provenance="generated",
        risk="R1",
    )
    result = h.session.run_step(step)
    assert result.gate == "confirm"
    assert result.ok is False
    assert result.evidence["detail"] == "screen_changed_during_act"
    assert [c[1] for c in h.physical.calls] == [EDIT[0]]
