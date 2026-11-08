"""Text sanitizer (design 5.9 [v0.8], [v1.0]): validated at the executor boundary before typing.

Rejects empty or whitespace-only text, control characters other than tab, lone surrogates,
invisible format characters that can spoof what the user sees (bidi controls, zero-width space,
BOM), and text over the cap measured in UTF-16 code units after newline normalisation. Newlines
(including U+2028/U+2029 and NEL) are never sent as Enter: they are rejected unless the app
profile declares a newline binding, which must be a valid, unblocked chord other than bare Enter
and other than the app's send chord (AX-01). Emoji joiners and variation selectors are allowed.

[v1.1] `sanitize_value` is the ValuePattern form: a value is content, not keystrokes, so it may be
empty and may hold newlines (normalised to LF); the character and length rules are the same.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from steerai.action.keyboard import chord_to_vks, is_blocked_combo, is_routed_combo
from steerai.errors import ErrorCode, SteerError

DEFAULT_MAX_UNITS = 4000
Part = tuple[str, str]
"""("text", literal) or ("key", chord)."""

_LF = chr(0x0A)
_CR = chr(0x0D)
_TAB = chr(0x09)
_ZERO_WIDTH_JOINER = chr(0x200D)
_ZERO_WIDTH_SPACE = chr(0x200B)
_ALLOWED_FORMAT = {_ZERO_WIDTH_JOINER} | {chr(cp) for cp in range(0xFE00, 0xFE10)}
_LINE_SEPARATORS = (_CR + _LF, _CR, chr(0x2028), chr(0x2029), chr(0x85))


@dataclass(frozen=True, slots=True)
class NewlineBinding:
    chord: str  # e.g. "shift+enter"

    def validate(self, *, send_chord: str | None = None) -> None:
        try:
            vks = chord_to_vks(self.chord)
        except SteerError as exc:
            raise SteerError(
                ErrorCode.INVALID_TEXT, f"newline binding {self.chord!r}: {exc.message}"
            ) from exc
        if vks == [0x0D]:
            raise SteerError(ErrorCode.INVALID_TEXT, "newline binding may not be bare Enter")
        if is_blocked_combo(self.chord) or is_routed_combo(self.chord):
            raise SteerError(ErrorCode.INVALID_TEXT, f"newline binding {self.chord!r} is blocked")
        if send_chord is not None and chord_to_vks(send_chord) == vks:
            raise SteerError(ErrorCode.INVALID_TEXT, "newline binding equals the app's send chord")


def _rejected(ch: str) -> bool:
    if ch in (_TAB, _LF):
        return False
    category = unicodedata.category(ch)
    if category in ("Cc", "Cs"):
        return True
    return category == "Cf" and ch not in _ALLOWED_FORMAT


def normalise_newlines(text: str) -> str:
    """CR LF, CR, NEL and the Unicode line and paragraph separators become LF."""
    for separator in _LINE_SEPARATORS:
        text = text.replace(separator, _LF)
    return text


def field_text(value: str, role: str) -> str:
    """A field's text as typed: newlines normalised, and for a rich text `Document` one final
    paragraph mark dropped. A rich text story always ends with one (measured 2026-11-08: an
    empty WordPad document reads a lone CR, #147); it is structure, never content."""
    text = normalise_newlines(value)
    if role == "Document" and text.endswith(_LF):
        return text[: -len(_LF)]
    return text


def _check_characters(normalised: str, max_units: int) -> None:
    bad = [ch for ch in normalised if _rejected(ch)]
    if bad:
        raise SteerError(
            ErrorCode.INVALID_TEXT,
            f"control or invisible characters are not typed: U+{ord(bad[0]):04X}",
        )
    units = len(normalised.encode("utf-16-le", errors="surrogatepass")) // 2
    if units > max_units:
        raise SteerError(
            ErrorCode.INVALID_TEXT, f"text is longer than the cap ({units} > {max_units} units)"
        )


def sanitize_value(text: str, *, max_units: int = DEFAULT_MAX_UNITS) -> str:
    """The value to hand to `ValuePattern.SetValue`: may be empty, newlines stay literal."""
    normalised = normalise_newlines(text)
    _check_characters(normalised, max_units)
    return normalised


def sanitize_text(
    text: str,
    *,
    newline: NewlineBinding | None = None,
    max_units: int = DEFAULT_MAX_UNITS,
    send_chord: str | None = None,
) -> list[Part]:
    normalised = normalise_newlines(text)
    if not normalised.replace(_ZERO_WIDTH_SPACE, "").strip():
        raise SteerError(ErrorCode.INVALID_TEXT, "text is empty or whitespace only")
    _check_characters(normalised, max_units)
    if _LF in normalised and newline is None:
        raise SteerError(
            ErrorCode.INVALID_TEXT, "newline in text and the app declares no newline binding"
        )
    if newline is None:
        return [("text", normalised)]
    newline.validate(send_chord=send_chord)
    parts: list[Part] = []
    for index, segment in enumerate(normalised.split(_LF)):
        if index > 0:
            parts.append(("key", newline.chord))
        if segment:
            parts.append(("text", segment))
    return parts
