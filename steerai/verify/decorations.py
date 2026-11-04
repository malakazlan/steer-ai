"""Name decorations (VF-34, design 5.10 [v1.7]).

Some apps show a step's content inside a fixed frame: a Google results page names its document
"<query> - Google Search". A plan names a decoration from this registry; code fills the frame
with the step's content parameter. The pattern the gate judges stays the bare content, so the
model never writes the frame and never verifies with a string of its own.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

CONTENT: Final = "{content}"
DECORATIONS: Final = MappingProxyType(
    {
        "google_search": "{content} - Google Search",
    }
)
"""Decoration key -> frame with exactly one `{content}` placeholder."""


def decorate(key: str, content: str) -> str:
    """The full name an element shows for `content` under `key`; unknown keys are refused."""
    frame = DECORATIONS.get(key)
    if frame is None:
        raise ValueError(f"unknown decoration {key!r}")
    return frame.replace(CONTENT, content)
