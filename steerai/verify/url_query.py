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


def page_loaded(  # noqa: PLR0913 - the page's parts and the before of record, by keyword
    document_names: Iterable[str],
    text: str,
    *,
    before: Iterable[str],
    url: str,
    document_url: str | None,
    url_before: UrlBefore,
) -> bool:
    """The load signal beside the address bar (5.10 [v2.51], amending [v2.48]): a Document name
    that was not among the before Documents' names and is not only the URL's host (an omnibox
    refinement shows the pending URL under the old title; a refused load is titled by the host),
    which either holds the typed words, case folded, or comes with a top Document URL that
    carries them by `UrlBefore.counts`. Chrome's error and certificate pages read
    `chrome-error://chromewebdata/` there, and a pending navigation keeps the old URL (probe
    `document_url`, n=5 each). An unreadable Document URL leaves the words rule alone."""
    wanted = _folded(text).casefold()
    host = (_split(url).hostname or "").casefold()
    hosts = {host, host.removeprefix("www.")}
    old = set(before)
    new = [
        _folded(name).casefold()
        for name in document_names
        if name not in old and _folded(name).casefold() not in hosts
    ]
    if not wanted or not new:
        return False
    if any(wanted in name for name in new):
        return True
    return document_url is not None and url_before.counts(document_url, text)
