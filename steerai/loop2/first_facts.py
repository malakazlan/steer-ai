"""The task's first observation, as one redacted trace record (3d shakedown 2026-11-15: the first
run saw the page; every later run read blank for the whole first-ready wait, even with the bound
window in front and uncovered, and the trace could not say why).

Closed tokens and counts only, never a name, a title or page text: how many Documents the last
read held (no single top Document: its node count is `none`), how many elements sit under the top
Document and in the whole read, whether the read was stable, whether the bound window was the
foreground, its cover token (`world.cover`), its window class as a closed token, the time from the
world's start to its first read, and how many reads the first observation took.
"""

from __future__ import annotations

import re
from typing import Final

from steerai.world.effects import Observed
from steerai.world.observe2 import document_nodes

CLASS_TOKEN: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
"""A window class kept as it is: ASCII letters, digits and `_ . : -`, at most 64 characters;
anything else is `class_other` (a class name is the program's, but never trusted as a token)."""


def class_token(name: str) -> str:
    """The window class as a closed token: itself when it fits `CLASS_TOKEN`, `class_unread`
    when empty, else `class_other`."""
    if not name:
        return "class_unread"
    return name if CLASS_TOKEN.fullmatch(name) else "class_other"


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "unread"
    return "yes" if value else "no"


def first_observation_record(  # noqa: PLR0913 - one record's facts, keyword-only
    seen: Observed | None,
    *,
    foreground: bool | None,
    cover: str | None,
    window_class: str,
    start_to_read_ms: float | None,
    reads: int,
) -> dict[str, str]:
    """The record; `seen` is None when the first observation ended before any read (a cover)."""
    if seen is None:
        documents = doc_nodes = nodes = stable = "none"
    else:
        snapshot = seen.snapshot
        inside = document_nodes(snapshot)
        documents = str(sum(e.role == "Document" for e in snapshot.elements))
        doc_nodes = "none" if inside is None else str(len(inside))
        nodes = str(len(snapshot.elements))
        stable = _yes_no(snapshot.stable)
    return {
        "event": "first_observation",
        "documents": documents,
        "doc_nodes": doc_nodes,
        "nodes": nodes,
        "stable": stable,
        "foreground": _yes_no(foreground),
        "cover": "none" if cover is None else cover,
        "window_class": class_token(window_class),
        "start_to_first_read_ms": (
            "none" if start_to_read_ms is None else str(round(start_to_read_ms))
        ),
        "reads": str(reads),
    }
