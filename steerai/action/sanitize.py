"""Text sanitizer (design 5.9 [v0.8]): validated at the executor boundary before any keystroke.

Rejects empty or whitespace-only text, control characters other than tab, and text over the cap.
Newlines are never sent as Enter: they are rejected unless the app profile declares a newline
binding, in which case each newline becomes that key chord between text parts.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from steerai.errors import ErrorCode, SteerError

DEFAULT_MAX_CHARS = 4000
Part = tuple[str, str]
"""("text", literal) or ("key", chord)."""


@dataclass(frozen=True, slots=True)
class NewlineBinding:
    chord: str  # e.g. "shift+enter"


def _is_control(ch: str) -> bool:
    return ch != "\t" and (unicodedata.category(ch) == "Cc")


def sanitize_text(
    text: str,
    *,
    newline: NewlineBinding | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[Part]:
    if not text.strip():
        raise SteerError(ErrorCode.INVALID_TEXT, "text is empty or whitespace only")
    if len(text) > max_chars:
        raise SteerError(
            ErrorCode.INVALID_TEXT, f"text is longer than the cap ({len(text)} > {max_chars})"
        )
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    bad = [ch for ch in normalised if ch != "\n" and _is_control(ch)]
    if bad:
        raise SteerError(ErrorCode.INVALID_TEXT, f"control characters are not typed: {bad[0]!r}")
    if "\n" in normalised and newline is None:
        raise SteerError(
            ErrorCode.INVALID_TEXT, "newline in text and the app declares no newline binding"
        )
    if newline is None:
        return [("text", normalised)]
    parts: list[Part] = []
    for index, segment in enumerate(normalised.split("\n")):
        if index > 0:
            parts.append(("key", newline.chord))
        if segment:
            parts.append(("text", segment))
    return parts
