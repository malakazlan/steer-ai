"""The batch rule (V2 §5; packet §3; [V2.2] condition 3): the next action runs only when the last
effect stayed on the acted element and the next target re-resolved the same. Red on the 3c stub."""

from __future__ import annotations

import pytest

from steerai.loop2.batch import batch_continues
from steerai.world.effects import EffectClass, EffectReport, Token, Unread
from tests.loop2.world_fakes import pending, report


@pending("§5 batch rule")
@pytest.mark.parametrize(
    "classes",
    [("own_state",), ("own_value",), ("focus",), ("own_value", "focus")],
)
def test_effects_on_the_acted_element_continue(classes: tuple[EffectClass, ...]) -> None:
    assert batch_continues(report(*classes), verified_type=False, same_target=True) is True


@pending("§5 batch rule")
def test_a_box_move_alone_continues() -> None:
    effect = report("own_value", tokens=["moved"])
    assert batch_continues(effect, verified_type=False, same_target=True) is True


@pending("§5 batch rule")
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


@pending("§5 batch rule")
@pytest.mark.parametrize(
    "token", ["frame_changed", "rerendered", "dismissed", "windows_changed", "title_only"]
)
def test_every_token_but_moved_ends_the_batch(token: Token) -> None:
    assert batch_continues(report(tokens=[token]), verified_type=True, same_target=True) is False


@pending("§5 batch rule")
@pytest.mark.parametrize("unread", ["url", "windows", "caps"])
def test_any_unread_fact_ends_the_batch(unread: Unread) -> None:
    effect = report("own_value", unread=[unread])
    assert batch_continues(effect, verified_type=True, same_target=True) is False


@pending("§5 batch rule")
def test_none_continues_after_a_verified_type() -> None:
    assert batch_continues(report("none"), verified_type=True, same_target=True) is True


@pending("§5 batch rule")
def test_none_ends_the_batch_without_a_verified_type() -> None:
    assert batch_continues(report("none"), verified_type=False, same_target=True) is False


@pending("§5 batch rule")
def test_a_target_that_no_longer_resolves_the_same_ends_the_batch() -> None:
    assert batch_continues(report("own_value"), verified_type=True, same_target=False) is False


def _swapped(effect: EffectReport) -> EffectReport:
    """The same report with another class as `primary`."""
    other = sorted(effect.classes)[-1]
    if other == effect.primary:
        other = sorted(effect.classes)[0]
    return report(*effect.classes, tokens=list(effect.tokens), primary=other)


@pending("[V2.2].3 nothing reads primary")
@pytest.mark.parametrize(
    "classes", [("own_value", "focus"), ("own_value", "content_changed"), ("focus", "revealed")]
)
def test_nothing_reads_primary(classes: tuple[EffectClass, ...]) -> None:
    effect = report(*classes)
    assert _swapped(effect).primary != effect.primary
    assert batch_continues(effect, verified_type=True, same_target=True) == batch_continues(
        _swapped(effect), verified_type=True, same_target=True
    )
