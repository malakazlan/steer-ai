"""The batch rule (V2 §5; packet §3; [V2.2] condition 3): the next action runs only when the last
effect stayed on the acted element and the next target re-resolved the same. Red on the 3c stub."""

from __future__ import annotations

import pytest

from steerai.loop2.batch import batch_continues
from steerai.loop2.guards import ActionKey, Strikes, TurnFacts
from steerai.loop2.query import query_outcome
from steerai.world.effects import EffectClass, EffectReport, Token, Unread
from tests.loop2.world_fakes import report


@pytest.mark.parametrize(
    "classes",
    [("own_state",), ("own_value",), ("focus",), ("own_value", "focus")],
)
def test_effects_on_the_acted_element_continue(classes: tuple[EffectClass, ...]) -> None:
    assert batch_continues(report(*classes), verified_type=False, same_target=True) is True


def test_a_box_move_alone_continues() -> None:
    effect = report("own_value", tokens=["moved"])
    assert batch_continues(effect, verified_type=False, same_target=True) is True


@pytest.mark.parametrize(
    "classes",
    [
        ("content_changed",),
        ("revealed",),
        ("navigated",),
        ("url_changed_in_place",),
        ("dialog_opened",),
        ("unknown",),
        ("own_value", "content_changed"),
    ],
)
def test_effects_beyond_the_acted_element_end_the_batch(classes: tuple[EffectClass, ...]) -> None:
    assert batch_continues(report(*classes), verified_type=True, same_target=True) is False


@pytest.mark.parametrize(
    "token", ["frame_changed", "rerendered", "dismissed", "windows_changed", "title_only"]
)
def test_every_token_but_moved_and_scrolled_ends_the_batch(token: Token) -> None:
    assert batch_continues(report(tokens=[token]), verified_type=True, same_target=True) is False


@pytest.mark.parametrize("unread", ["url", "windows", "caps"])
def test_any_unread_fact_ends_the_batch(unread: Unread) -> None:
    effect = report("own_value", unread=[unread])
    assert batch_continues(effect, verified_type=True, same_target=True) is False


def test_none_continues_after_a_verified_type() -> None:
    assert batch_continues(report("none"), verified_type=True, same_target=True) is True


def test_none_ends_the_batch_without_a_verified_type() -> None:
    assert batch_continues(report("none"), verified_type=False, same_target=True) is False


def test_a_target_that_no_longer_resolves_the_same_ends_the_batch() -> None:
    assert batch_continues(report("own_value"), verified_type=True, same_target=False) is False


def _swapped(effect: EffectReport) -> EffectReport:
    """The same report with another class as `primary`."""
    other = sorted(effect.classes)[-1]
    if other == effect.primary:
        other = sorted(effect.classes)[0]
    return report(*effect.classes, tokens=list(effect.tokens), primary=other)


@pytest.mark.parametrize(
    "classes", [("own_value", "focus"), ("own_value", "content_changed"), ("focus", "revealed")]
)
def test_nothing_reads_primary(classes: tuple[EffectClass, ...]) -> None:
    """[V2.2] condition 3: the batch rule, the query outcomes and the repeat rule read the class
    set, never `primary`."""
    effect = report(*classes)
    swapped = _swapped(effect)
    assert swapped.primary != effect.primary
    assert batch_continues(effect, verified_type=True, same_target=True) == batch_continues(
        swapped, verified_type=True, same_target=True
    )
    for carried in (True, False):
        assert query_outcome(effect, text_carried=carried) == query_outcome(
            swapped, text_carried=carried
        )
    present = frozenset({(9, 3)})
    for verb in ("click", "press"):
        key = ActionKey(verb=verb, params=(), runtime_id=(9, 3), identity=("Button", "go", ""))
        sent = []
        for seen in (effect, swapped):
            strikes = Strikes(limit=3)
            strikes.record(
                TurnFacts(
                    ticked=False,
                    action=key,
                    effect=seen,
                    state_digest="d1",
                    claim_rejected=False,
                    present=present,
                )
            )
            sent.append(strikes.may_send(key, present=present))
        assert sent[0] == sent[1]
