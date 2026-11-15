"""The v2 listing's cap ([V2.54], [V2.55]; packet rev 4.1): which elements of a snapshot the
model is shown, in tree order, with each row's class.

0. **The root chain** (`root`): the top Document and its ancestors up to the window, shown
   first and charged to the cap and to the listing's `2 * cap` ceiling, never to a class's
   sub-limit (#360: charged to the first class that reached it, a deep chain emptied the goal
   class). A chain longer than the cap, or no single top Document, is left to the classes.
1. **Always-keep** (`keep`), up to `2 * cap` rows with the root chain: the keyboard focus, every
   element acted on in this task while it exists, the fields holding pending text, and what is
   new or changed since the model last looked. Chosen by v1's order (on-screen interactive
   first); `keep_at_ceiling` says the class passed the cap or an element of it was cut.
2. **Goal words** (`goal`), up to `cap // 3` rows: elements whose name holds a word of an open
   subgoal's params (the person's own words), most distinct words first, then v1's order;
   `goal_at_subcap` says the class was cut.
3. **v1's prune** (`rest`) for what is left of `cap`: on-screen interactive and named elements
   with their ancestors, then offscreen elements nearest the window (`world.elements.prune`).

The listing is closed upward (#359 review): an element is shown with its whole chain of
ancestors up to the nearest row already shown, and every row so added below the root chain is
charged to that element's class, so no class passes its budget and a row's indentation is its
true depth. An element whose chain does not fit its class's room is cut, never shown orphaned.
A cut element
keeps its task handle but has no row: `validate` refuses it as `id_not_listed`. The verifier and
the auto proofs read the full snapshot, never this.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from steerai.world.effects import top_document
from steerai.world.elements import INTERACTIVE_ROLES, Element, prune
from steerai.world.model import Snapshot

LISTING_CAP: Final = 150
"""[V2.54]: v1's `DEFAULT_CAP`; configurable, provisional."""
RowClass = Literal["root", "keep", "goal", "rest"]
"""Why a row is shown; a prompt guard drops `rest` first, then `goal`, then `keep`, never
`root`."""
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

    def note(self, prompt_cut: int = 0) -> str | None:
        """The turn's header line when anything was cut ([V2.54]): the rows actually sent (after
        the prompt guard cut `prompt_cut` of them), the cap and both sub-limits; whether the
        always-keep class alone was over the cap; and the guard's own cut."""
        shown = len(self.ids) - prompt_cut
        if shown >= self.eligible and not self.keep_at_ceiling:
            return None
        line = (
            f"listing: shown {shown} of {self.eligible} (cap {self.cap}, always-keep up "
            f"to {2 * self.cap}, goal words up to {self.cap // 3})"
        )
        if self.keep_at_ceiling:
            line = f"{line}; always-keep over cap"
        return f"{line}; the prompt size limit cut {prompt_cut}" if prompt_cut else line


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
    shown: dict[int, RowClass] = {}
    top = top_document(snapshot)
    if top is not None:
        _fill([top], shown, by_id, "root", cap)
    keep_cut = _fill(keep_all, shown, by_id, "keep", 2 * cap - len(shown))
    keep_rows = len(shown)
    goal_all = [e for e in elements if goal_words and _holds_word(e, goal_words)]
    goal_all.sort(key=lambda e: _goal_rank(e, goal_words, order))
    goal_cut = _fill(goal_all, shown, by_id, "goal", max(0, min(cap // 3, cap - keep_rows)))
    rest = [by_id[i] for i in sorted(order, key=order.__getitem__)]
    _fill(rest, shown, by_id, "rest", cap - len(shown))
    ids = tuple(e.id for e in elements if e.id in shown)
    eligible = len(set(order) | {e.id for e in keep_all} | {e.id for e in goal_all} | set(shown))
    return Listing(
        ids=ids,
        priorities=tuple(shown[i] for i in ids),
        eligible=eligible,
        cap=cap,
        keep_at_ceiling=keep_rows > cap or keep_cut > 0,
        goal_at_subcap=goal_cut > 0,
    )


def _fill(
    candidates: list[Element],
    shown: dict[int, RowClass],
    by_id: dict[int, Element],
    row_class: RowClass,
    room: int,
) -> int:
    """Show each candidate in turn with its ancestors up to the nearest shown row, all charged to
    `row_class`'s `room`; one whose chain does not fit is cut (and the next is tried). Returns how
    many candidates were cut."""
    cut = 0
    for element in candidates:
        if element.id in shown:
            continue
        chain = _unshown_chain(element, shown, by_id)
        if len(chain) > room:
            cut += 1
            continue
        for member in chain:
            shown[member.id] = row_class
        room -= len(chain)
    return cut


def _unshown_chain(
    element: Element, shown: dict[int, RowClass], by_id: dict[int, Element]
) -> list[Element]:
    """`element` and its ancestors up to the nearest shown one; a parent cycle in a hostile tree
    ends at the first repeat."""
    chain = [element]
    seen = {element.id}
    parent_id = element.parent_id
    while parent_id is not None and parent_id in by_id and parent_id not in shown:
        if parent_id in seen:
            break
        seen.add(parent_id)
        parent = by_id[parent_id]
        chain.append(parent)
        parent_id = parent.parent_id
    return chain


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


def _goal_rank(
    element: Element, words: frozenset[str], order: dict[int, int]
) -> tuple[int, int, int]:
    """#359 review (2): the goal class by the number of distinct goal words matched, most first,
    then v1's order: a row that names the target outranks a run of rows sharing one word."""
    matched = len(words & _words(element))
    return (-matched, order.get(element.id, len(order)), element.id)


def _holds_word(element: Element, words: frozenset[str]) -> bool:
    return not words.isdisjoint(_words(element))


def _words(element: Element) -> frozenset[str]:
    return frozenset(w.casefold() for w in _WORD.findall(element.name))
