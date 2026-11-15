"""[V2.54], [V2.55]: the v2 listing's cap. Always-keep first (up to twice the cap), then the
goal-word class (up to a third of the cap), then v1's prune order for the rest. A cut element
keeps its task handle but has no row, so the model cannot name it."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.loop2.listing import LISTING_CAP, Listing, select_listing
from steerai.world.elements import Element, prune
from steerai.world.model import Snapshot
from tests.loop2.world_fakes import snapshot

ROOT = Element(id=0, depth=0, role="Document", name="Big", box=(0, 0, 1000, 800), runtime_id=(1, 0))


def _row(i: int, *, name: str = "", role: str = "Button", top: int | None = None,
         parent: int = 0, offscreen: bool = False) -> Element:  # fmt: skip
    y = 10 * i if top is None else top
    return Element(
        id=i,
        depth=1,
        role=role,
        name=name or f"Item {i}",
        box=(0, y, 100, y + 8),
        runtime_id=(1, i),
        parent_id=parent,
        offscreen=offscreen,
    )


def _page(n: int, **extra: Element) -> Snapshot:
    rows = [_row(i) for i in range(1, n + 1)]
    return snapshot([ROOT, *rows, *extra.values()])


def _select(shown: Snapshot, **kw: object) -> Listing:
    return select_listing(shown, **kw)  # type: ignore[arg-type]  # keyword overrides per test


def test_a_page_under_the_cap_lists_what_v1_would() -> None:
    shown = _page(20)
    listing = _select(shown, cap=150)
    expected = prune(shown.elements, cap=len(shown.elements), offscreen=True)
    assert listing.ids == tuple(e.id for e in expected)
    assert listing.cut == 0
    assert listing.eligible == len(expected)


def test_the_default_cap_is_v1s_150() -> None:
    assert LISTING_CAP == 150


def test_a_large_page_is_cut_to_the_cap_in_v1s_order() -> None:
    shown = _page(400)
    listing = _select(shown, cap=150)
    assert len(listing.ids) == 150
    assert listing.cut == 401 - 150
    v1 = [e.id for e in prune(shown.elements, cap=150, offscreen=True)]
    assert listing.ids == tuple(v1)


def test_always_keep_comes_first_whatever_v1_would_cut() -> None:
    shown = _page(400)
    far = (1, 390)
    listing = _select(shown, cap=150, keep=frozenset({far}))
    assert 390 in listing.ids
    assert len(listing.ids) == 150


def test_the_focus_is_always_kept() -> None:
    shown = _page(400)
    listing = _select(shown, cap=150, focus=(1, 380))
    assert 380 in listing.ids


def test_always_keep_over_the_cap_stops_at_twice_the_cap_and_says_so() -> None:
    shown = _page(400)
    every = frozenset((1, i) for i in range(1, 401))
    listing = _select(shown, cap=100, keep=every)
    assert len([i for i in listing.ids if i != 0]) <= 200
    assert listing.keep_at_ceiling is True


def test_an_always_keep_set_inside_the_cap_is_not_at_its_ceiling() -> None:
    listing = _select(_page(400), cap=100, keep=frozenset({(1, 5)}))
    assert listing.keep_at_ceiling is False


def test_goal_words_come_next_up_to_a_third_of_the_cap() -> None:
    lamps = [_row(1000 + i, name=f"Brass lamp {i}", top=5000 + i) for i in range(80)]
    shown = snapshot([ROOT, *(_row(i) for i in range(1, 400)), *lamps])
    listing = _select(shown, cap=90, goal_words=frozenset({"lamp"}))
    kept_lamps = [i for i in listing.ids if i >= 1000]
    assert len(kept_lamps) == 90 // 3
    assert listing.goal_at_subcap is True
    assert len(listing.ids) == 90


def test_a_goal_word_matches_whole_words_case_folded() -> None:
    shown = snapshot([ROOT, _row(1, name="LAMP"), _row(2, name="Clampdown"), *(
        _row(i) for i in range(3, 400))])  # fmt: skip
    listing = _select(shown, cap=10, goal_words=frozenset({"lamp"}))
    by_id = dict(zip(listing.ids, listing.priorities, strict=True))
    assert by_id[1] == "goal"
    assert by_id.get(2) != "goal"  # a word inside another word is not the word


def test_an_over_full_always_keep_class_is_chosen_on_screen_interactive_first() -> None:
    """[V2.55] (1): past the ceiling, v1's order inside the class, never tree order."""
    hidden = [_row(i, top=5000 + i, offscreen=True) for i in range(1, 6)]
    plain = [_row(i, role="Text", name=f"Note {i}") for i in range(6, 11)]
    buttons = [_row(i) for i in range(11, 16)]
    shown = snapshot([ROOT, *hidden, *plain, *buttons])
    every = frozenset(e.runtime_id for e in [*hidden, *plain, *buttons])
    listing = _select(shown, cap=2, keep=every)
    kept = {i for i, c in zip(listing.ids, listing.priorities, strict=True) if c == "keep"}
    assert set(range(11, 15)) <= kept  # on-screen interactive first, up to the ceiling of 4
    assert not kept & set(range(1, 6))


def test_a_kept_element_brings_its_parent_so_indentation_stays_true() -> None:
    group = Element(id=500, depth=1, role="Group", name="Menu", box=(0, 5000, 100, 5100),
                    runtime_id=(1, 500), parent_id=0)  # fmt: skip
    item = dataclasses.replace(_row(501, top=5010, parent=500), depth=2)
    shown = snapshot([ROOT, *(_row(i) for i in range(1, 400)), group, item])
    listing = _select(shown, cap=50, keep=frozenset({item.runtime_id}))
    assert {500, 501} <= set(listing.ids)


def test_rows_are_in_tree_order() -> None:
    shown = _page(400)
    listing = _select(shown, cap=150, keep=frozenset({(1, 390)}))
    assert list(listing.ids) == sorted(listing.ids)


def test_priorities_say_what_a_prompt_guard_may_drop_first() -> None:
    lamp = _row(700, name="Brass lamp", top=7000)
    shown = snapshot([ROOT, *(_row(i) for i in range(1, 400)), lamp])
    listing = _select(shown, cap=60, keep=frozenset({(1, 390)}), goal_words=frozenset({"lamp"}))
    by_id = dict(zip(listing.ids, listing.priorities, strict=True))
    assert by_id[390] == "keep"
    assert by_id[700] == "goal"
    assert by_id[1] == "rest"


@pytest.mark.parametrize("cap", [0, -1])
def test_a_cap_below_one_is_refused(cap: int) -> None:
    with pytest.raises(ValueError, match="cap"):
        _select(_page(3), cap=cap)
