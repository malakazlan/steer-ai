"""The address bar as a search submit's proof on Chrome (#148, design D): pure over the omnibox
values read before and after the submit. The omnibox hides the scheme and shows spaces as `+`
(probe #157). The text must fill one whole query parameter exactly: an autocompleted "<q> radio"
or a page that echoes part of it never counts."""

from __future__ import annotations

import unicodedata
import urllib.parse


def _params(value: str) -> list[tuple[str, str]]:
    url = value if "://" in value else "https://" + value
    return urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)


def _folded(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def carries(value: str, text: str) -> bool:
    """Whether one query parameter of the omnibox value is exactly the text."""
    wanted = _folded(text)
    return bool(wanted) and any(_folded(v) == wanted for _k, v in _params(value))


def url_query_counts(before: str, after: str, text: str) -> bool:
    """The URL changed, the before URL did not already carry the text, and the after URL does."""
    return bool(after) and after != before and not carries(before, text) and carries(after, text)
