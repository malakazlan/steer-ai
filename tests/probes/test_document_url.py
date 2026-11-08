"""The document-URL probe's pure part: the scheme of a Document value."""

from __future__ import annotations

from steerai.probes.document_url import scheme_of


def test_scheme_of_reads_the_scheme_or_nothing() -> None:
    assert scheme_of("https://developer.mozilla.org/en-US/search?q=flexbox") == "https"
    assert scheme_of("chrome-error://chromewebdata/") == "chrome-error"
    assert scheme_of("developer.mozilla.org/en-US/") == ""
    assert scheme_of("") == ""
