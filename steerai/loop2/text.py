"""One-line cleaning for any text that is shown to the model or the user."""

from __future__ import annotations

import unicodedata


def has_control(text: str) -> bool:
    """True if the text holds any Unicode C* character (control, format, surrogate, unassigned)."""
    return any(unicodedata.category(c).startswith("C") for c in text)


def clean_line(text: str) -> str:
    """Fold all whitespace and line boundaries to single spaces and drop C* characters."""
    folded = " ".join(text.split())
    kept = "".join(c for c in folded if not unicodedata.category(c).startswith("C"))
    return " ".join(kept.split())
