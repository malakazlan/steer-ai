"""How many model calls this process made, counted at the SDK boundary (Phase 4 bar, gate item
6): the planner, goal-match and the element chooser each count one per `messages.parse` call."""

from __future__ import annotations

from collections import Counter

_CALLS: Counter[str] = Counter()


def count(kind: str) -> None:
    _CALLS[kind] += 1


def snapshot() -> dict[str, int]:
    return dict(_CALLS)


def reset() -> None:
    _CALLS.clear()
