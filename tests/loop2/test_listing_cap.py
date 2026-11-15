"""[V2.54], [V2.55]: the v2 listing's cap. Always-keep first (up to twice the cap), then the
goal-word class (up to a third of the cap), then v1's prune order for the rest. A cut element
keeps its task handle but has no row, so the model cannot name it."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.loop2.listing import LISTING_CAP, Listing, select_listing, words_of
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
    assert listing.priorities.count("goal") == 90 // 3  # the lamps and the root they bring
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
    assert kept == {0, 11, 12, 13}  # on-screen interactive first; the root is charged too
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


# ---- #359 review (1): every added row is charged to its class; the listing is closed upward ----


def _grouped(n: int, *, name: str = "Item {i}") -> Snapshot:
    """Each button alone in its own group: a kept or goal button costs two rows."""
    elements = [ROOT]
    for g in range(n):
        box = (0, 10 * g, 100, 10 * g + 8)
        group = Element(id=10_000 + g, depth=1, role="Group", name=f"g{g}", box=box,
                        runtime_id=(1, 10_000 + g), parent_id=0)  # fmt: skip
        button = Element(id=20_000 + g, depth=2, role="Button", name=name.format(i=g), box=box,
                         runtime_id=(1, 20_000 + g), parent_id=group.id)  # fmt: skip
        elements += [group, button]
    return snapshot(elements)


def _closed_upward(shown: Snapshot, listing: Listing) -> bool:
    """Every shown row's parent is shown: a row's indentation is its true depth."""
    by_id = {e.id: e for e in shown.elements}
    ids = set(listing.ids)
    return all(by_id[i].parent_id is None or by_id[i].parent_id in ids for i in ids)


def test_always_keep_with_its_ancestors_never_passes_twice_the_cap() -> None:
    shown = _grouped(400)
    every = frozenset((1, 20_000 + g) for g in range(400))
    listing = _select(shown, cap=150, keep=every)
    assert len(listing.ids) <= 300
    assert listing.keep_at_ceiling is True
    assert _closed_upward(shown, listing)


def test_goal_rows_with_their_ancestors_never_pass_a_third_of_the_cap() -> None:
    shown = _grouped(400, name="Red shoes {i}")
    listing = _select(shown, cap=150, goal_words=frozenset({"red"}))
    assert listing.priorities.count("goal") <= 150 // 3
    assert listing.goal_at_subcap is True
    assert len(listing.ids) <= 150
    assert _closed_upward(shown, listing)


def _deep(depth: int) -> list[Element]:
    """A chain of groups down to a button at `depth`, far below the fold."""
    chain = []
    parent = 0
    for d in range(1, depth + 1):
        role = "Button" if d == depth else "Group"
        chain.append(Element(id=900 + d, depth=d, role=role, name=f"Deep {d}",
                             box=(0, 9000 + d, 100, 9008 + d), runtime_id=(1, 900 + d),
                             parent_id=parent, offscreen=True))  # fmt: skip
        parent = 900 + d
    return chain


def test_a_deep_kept_element_brings_its_whole_chain_to_the_nearest_shown_row() -> None:
    chain = _deep(6)
    shown = snapshot([ROOT, *(_row(i) for i in range(1, 400)), *chain])
    listing = _select(shown, cap=50, keep=frozenset({chain[-1].runtime_id}))
    assert {e.id for e in chain} <= set(listing.ids)
    assert len(listing.ids) == 50
    assert _closed_upward(shown, listing)


def test_an_element_whose_chain_does_not_fit_its_class_is_cut_never_orphaned() -> None:
    chain = _deep(6)  # the button needs 7 rows with the root; the ceiling at cap 2 is 4
    shown = snapshot([ROOT, *(_row(i) for i in range(1, 400)), *chain])
    listing = _select(shown, cap=2, keep=frozenset({chain[-1].runtime_id}))
    assert chain[-1].id not in listing.ids
    assert listing.keep_at_ceiling is True
    assert _closed_upward(shown, listing)


def test_an_element_that_does_not_fit_leaves_room_for_the_next() -> None:
    chain = [dataclasses.replace(e, offscreen=False) for e in _deep(6)]  # ranked first, too deep
    far = _row(390, top=5000, offscreen=True)
    shown = snapshot([ROOT, *(_row(i) for i in range(1, 390)), far, *chain])
    listing = _select(shown, cap=2, keep=frozenset({chain[-1].runtime_id, far.runtime_id}))
    assert chain[-1].id not in listing.ids
    assert far.id in listing.ids


@pytest.mark.parametrize("cap", [3, 10, 40, 150])
def test_every_listing_is_closed_upward_whatever_the_classes(cap: int) -> None:
    shown = _grouped(200, name="Red item {i}")
    some = frozenset((1, 20_000 + g) for g in range(0, 200, 7))
    listing = _select(shown, cap=cap, keep=some, goal_words=frozenset({"red"}))
    assert _closed_upward(shown, listing)
    assert len(listing.ids) <= 2 * cap


def test_a_parent_cycle_in_a_hostile_tree_ends() -> None:
    a = Element(id=1, depth=1, role="Button", name="A", box=(0, 0, 9, 9), runtime_id=(1, 1),
                parent_id=2)  # fmt: skip
    b = Element(id=2, depth=1, role="Button", name="B", box=(0, 9, 9, 18), runtime_id=(1, 2),
                parent_id=1)  # fmt: skip
    listing = _select(snapshot([a, b]), cap=5, keep=frozenset({(1, 1)}))
    assert 1 in listing.ids


# ---- #359 review (2): the goal class ranks by distinct goal words, then v1's order -------------


def test_the_target_on_the_large_page_survives_two_thousand_one_word_rows() -> None:
    """The shape of `/large.html`: 2,000 `Item N` rows each with a `Details` button, and the goal
    `reserve item 1987`. Tree order filled the class with `Item N` and cut the target."""
    elements = [ROOT, Element(id=999, depth=1, role="List", name="", box=(0, 0, 800, 20_000),
                              runtime_id=(1, 999), parent_id=0)]  # fmt: skip
    for i in range(1, 2001):
        box = (0, 10 * i, 800, 10 * i + 8)
        row_id = 1000 + 2 * i
        elements.append(Element(id=row_id, depth=2, role="ListItem", name=f"Item {i}",
                                box=box, offscreen=i > 60, runtime_id=(1, row_id),
                                parent_id=999))  # fmt: skip
        name = "Reserve item 1987" if i == 1987 else "Details"
        elements.append(Element(id=row_id + 1, depth=3, role="Button", name=name, box=box,
                                offscreen=i > 60, runtime_id=(1, row_id + 1),
                                parent_id=row_id))  # fmt: skip
    shown = snapshot(elements)
    listing = _select(shown, goal_words=words_of(["reserve item 1987"]))
    target = 1000 + 2 * 1987 + 1
    by_id = dict(zip(listing.ids, listing.priorities, strict=True))
    assert by_id.get(target) == "goal"
    assert listing.goal_at_subcap is True


def test_more_distinct_goal_words_come_first_then_v1s_order() -> None:
    ones = [_row(i, name=f"Lamp {i}") for i in range(1, 6)]
    two = _row(300, name="Brass lamp", top=3000, offscreen=True)
    filler = [_row(i, name=f"Other {i}") for i in range(6, 200)]
    shown = snapshot([ROOT, *ones, *filler, two])
    listing = _select(
        shown, cap=9, keep=frozenset({(1, 6)}), goal_words=frozenset({"brass", "lamp"})
    )
    goal = [i for i, c in zip(listing.ids, listing.priorities, strict=True) if c == "goal"]
    assert goal == [1, 2, 300]  # two words first; then the one-word rows in v1's order
