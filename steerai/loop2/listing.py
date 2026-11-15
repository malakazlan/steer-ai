"""The v2 listing's cap ([V2.54], [V2.55]; packet rev 4.1): which elements of a snapshot the
model is shown, in tree order, with each row's class.

1. **Always-keep** (`keep`), up to `2 * cap`: the keyboard focus, every element acted on in this
   task while it exists, the fields holding pending text, and (when the lead rules) what is new
   since the last turn. Over the ceiling, chosen by v1's order (on-screen interactive first);
   `keep_at_ceiling` says so.
2. **Goal words** (`goal`), up to `cap // 3`: elements whose name holds a word of an open
   subgoal's params (the person's own words); `goal_at_subcap` says the class was cut.
3. **v1's prune** (`rest`) for what is left of `cap`: on-screen interactive and named elements
   with their ancestors, then offscreen elements nearest the window (`world.elements.prune`).

A kept element brings its parent, so a row's indentation stays true. A cut element keeps its task
handle but has no row: `validate` refuses it as `id_not_listed`. The verifier and the auto proofs
read the full snapshot, never this.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from steerai.world.elements import INTERACTIVE_ROLES, Element, prune
from steerai.world.model import Snapshot

LISTING_CAP: Final = 150
"""[V2.54]: v1's `DEFAULT_CAP`; configurable, provisional."""
RowClass = Literal["keep", "goal", "rest"]
"""Why a row is shown; a prompt guard drops `rest` first, then `goal`, then `keep`."""
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class Listing:
    """The rows to show, by snapshot id in tree order, and the per-turn counts ([V2.54])."""

    ids: tuple[int, ...]
    priorities: tuple[RowClass, ...]
    eligible: int
    """What the uncapped listing would show: v1's prune with no cap, plus always-keep and goal."""
    cap: int
    keep_at_ceiling: bool
    goal_at_subcap: bool

    @property
    def cut(self) -> int:
        return max(0, self.eligible - len(self.ids))

    def turn(
        self, listed: frozenset[int], prompt_cut: int, prompt_tokens: int | None
    ) -> ListingTurn:
        """This listing as one turn's record, after the prompt guard."""
        return ListingTurn(
            eligible=self.eligible,
            shown=len(self.ids),
            cut=self.cut,
            keep_at_ceiling=self.keep_at_ceiling,
            goal_at_subcap=self.goal_at_subcap,
            prompt_cut=prompt_cut,
            prompt_tokens=prompt_tokens,
            listed=listed,
        )

    def note(self) -> str | None:
        """The turn's header line when anything was cut ([V2.54]): the counts, the cap and both
        sub-limits; and whether the always-keep class alone was over the cap."""
        if self.cut == 0 and not self.keep_at_ceiling:
            return None
        line = (
            f"listing: shown {len(self.ids)} of {self.eligible} (cap {self.cap}, always-keep up "
            f"to {2 * self.cap}, goal words up to {self.cap // 3})"
        )
        return f"{line}; always-keep over cap" if self.keep_at_ceiling else line


@dataclass(frozen=True)
class ListingTurn:
    """[V2.54] the per-turn counts for the stage 1 report: eligible, shown, cut, whether
    always-keep reached its ceiling and goal words their sub-cap, the rows the prompt guard cut
    ([V2.55] (2)), the prompt's input tokens as reported, and the handles the model was shown
    (the set its Decision is validated against)."""

    eligible: int
    shown: int
    cut: int
    keep_at_ceiling: bool
    goal_at_subcap: bool
    prompt_cut: int
    prompt_tokens: int | None
    listed: frozenset[int]


def words_of(texts: Iterable[str]) -> frozenset[str]:
    """The case-folded words of the given texts (an open subgoal's param words)."""
    return frozenset(w.casefold() for text in texts for w in _WORD.findall(text))


def select_listing(
    snapshot: Snapshot,
    *,
    cap: int = LISTING_CAP,
    focus: tuple[int, ...] | None = None,
    keep: frozenset[tuple[int, ...]] = frozenset(),
    goal_words: frozenset[str] = frozenset(),
) -> Listing:
    if cap < 1:
        raise ValueError("cap must be >= 1")
    elements = snapshot.elements
    by_id = {e.id: e for e in elements}
    order = _v1_order(elements)
    wanted_keep = keep | ({focus} if focus else set())
    keep_all = [e for e in elements if e.runtime_id and e.runtime_id in wanted_keep]
    keep_all.sort(key=lambda e: _keep_rank(e, order))
    keep_ceiling = 2 * cap
    kept = keep_all[:keep_ceiling]
    goal_all = [
        e
        for e in elements
        if goal_words and e.id not in {k.id for k in kept} and _holds_word(e, goal_words)
    ]
    goal_room = max(0, min(cap // 3, cap - len(kept)))
    goal = goal_all[:goal_room]
    classes: dict[int, RowClass] = {}
    for e in kept:
        classes[e.id] = "keep"
    for e in goal:
        classes.setdefault(e.id, "goal")
    for e in [*kept, *goal]:
        parent = by_id.get(e.parent_id) if e.parent_id is not None else None
        if parent is not None:
            classes.setdefault(parent.id, classes[e.id])
    room = cap - len(classes)
    for element_id in sorted(order, key=order.__getitem__):
        if room <= 0:
            break
        if element_id not in classes:
            classes[element_id] = "rest"
            room -= 1
    ids = tuple(e.id for e in elements if e.id in classes)
    eligible = len(set(order) | {e.id for e in keep_all} | {e.id for e in goal_all})
    return Listing(
        ids=ids,
        priorities=tuple(classes[i] for i in ids),
        eligible=eligible,
        cap=cap,
        keep_at_ceiling=len(keep_all) > cap,
        goal_at_subcap=len(goal_all) > len(goal),
    )


def _v1_order(elements: list[Element]) -> dict[int, int]:
    """v1's prune with no cap, as a rank: on-screen interactive and named elements with their
    ancestors in tree order, then offscreen ones nearest the window."""
    full = prune(elements, cap=max(len(elements), 1), offscreen=True)
    return {e.id: rank for rank, e in enumerate(full)}


def _keep_rank(element: Element, order: dict[int, int]) -> tuple[bool, bool, int, int]:
    """[V2.55] (1): inside an over-full always-keep class, v1's order: on-screen interactive
    first, then the rest by prune's rank."""
    return (
        element.offscreen,
        element.role not in INTERACTIVE_ROLES,
        order.get(element.id, len(order)),
        element.id,
    )


def _holds_word(element: Element, words: frozenset[str]) -> bool:
    return not words.isdisjoint(w.casefold() for w in _WORD.findall(element.name))
