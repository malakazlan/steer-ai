"""The address bar as a search submit's proof on Chrome (#148, design D): pure over the omnibox
values read before and after the submit. The omnibox hides the scheme and shows spaces as `+`
(probe #157). The text must fill one whole query parameter exactly: an autocompleted "<q> radio"
or a page that echoes part of it never counts."""

from __future__ import annotations

import hashlib
import unicodedata
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass


def _split(value: str) -> urllib.parse.SplitResult:
    return urllib.parse.urlsplit(value if "://" in value else "https://" + value)


def _params(value: str) -> list[tuple[str, str]]:
    return urllib.parse.parse_qsl(_split(value).query, keep_blank_values=True)


def _folded(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def carries(value: str, text: str) -> bool:
    """Whether one query parameter of the omnibox value is exactly the text."""
    wanted = _folded(text)
    return bool(wanted) and any(_folded(v) == wanted for _k, v in _params(value))


def names_the_words(
    document_names: Iterable[str], text: str, *, before: Iterable[str], url: str
) -> bool:
    """The load signal beside the URL: a Document whose name holds the typed words, case
    folded, that was not among the before Documents' names and is not only the URL's host.
    Chrome keeps the URL on its error page (titled by the host) and shows an address-bar
    navigation's URL before the server answers, under the old page's title (probe #157
    `commit`; #159 review item 1)."""
    wanted = _folded(text).casefold()
    host = (_split(url).hostname or "").casefold()
    hosts = {host, host.removeprefix("www.")}
    old = set(before)
    return bool(wanted) and any(
        name not in old
        and _folded(name).casefold() not in hosts
        and wanted in _folded(name).casefold()
        for name in document_names
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class UrlBefore:
    """What the ledger keeps of the before URL (design D item 5): its sha256 and whether it
    already carried the text, never the URL itself."""

    digest: str
    carried: bool

    @classmethod
    def of(cls, value: str, text: str) -> UrlBefore:
        return cls(digest=_digest(value), carried=carries(value, text))

    def counts(self, after: str, text: str) -> bool:
        """The URL changed, the before URL did not carry the text, and the after URL does."""
        return (
            bool(after)
            and _digest(after) != self.digest
            and not self.carried
            and carries(after, text)
        )


def url_query_counts(before: str, after: str, text: str) -> bool:
    """The live form of `UrlBefore.counts`, over the before URL itself."""
    return UrlBefore.of(before, text).counts(after, text)
