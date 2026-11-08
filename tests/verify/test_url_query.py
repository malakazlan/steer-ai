"""A search submit on Chrome counts when the address bar shows its words (#148, design D): the
URL changed, the before URL did not already carry the text, and one query parameter equals the
typed text exactly after decoding, NFKC and whitespace folding, case kept; a missing scheme is
allowed (the omnibox hides it, probe #157)."""

from __future__ import annotations

import pytest

from steerai.verify.url_query import UrlBefore, page_loaded, url_query_counts

BEFORE = "www.example.org/"
TEXT = "ui automation tree"


def test_om01_a_get_query_with_the_words_counts() -> None:
    assert url_query_counts(BEFORE, "www.example.org/search?q=ui+automation+tree", TEXT)


def test_om02_an_autocompleted_query_never_counts() -> None:
    assert not url_query_counts(BEFORE, "www.example.org/search?q=ui+automation+tree+radio", TEXT)


def test_om03_the_same_query_twice_is_not_an_effect() -> None:
    url = "www.example.org/search?q=ui+automation+tree"
    assert not url_query_counts(url, url, TEXT), "unchanged URL"
    assert not url_query_counts(url, url + "&page=2", TEXT), "before already carried the text"


def test_om06_encodings_unicode_and_a_missing_scheme_count() -> None:
    assert url_query_counts(BEFORE, "https://x.org/s?q=ui%20automation%20tree", TEXT)
    assert url_query_counts(BEFORE, "x.org/s?p=caf%C3%A9+cr%C3%A8me", "café crème")
    assert url_query_counts(BEFORE, "x.org/s?q=ui++automation%09tree", TEXT), "spacing folded"
    assert not url_query_counts(BEFORE, "x.org/s?q=UI+Automation+Tree", TEXT), "case kept"


def test_om11_a_post_form_leaves_the_url_unchanged() -> None:
    assert not url_query_counts(BEFORE, BEFORE, TEXT)


def test_om12_a_redirect_without_the_query_never_counts() -> None:
    assert not url_query_counts(BEFORE, "en.wikipedia.org/wiki/UI_Automation", TEXT)


def test_the_words_must_fill_a_whole_parameter_value() -> None:
    assert not url_query_counts(BEFORE, "x.org/s?q=tree&r=ui+automation", TEXT)
    assert not url_query_counts(BEFORE, "x.org/s?ui+automation+tree", TEXT), "a bare key"
    assert not url_query_counts(BEFORE, "", TEXT)
    assert not url_query_counts(BEFORE, "x.org/s?q=", "")


def test_om08_the_before_url_is_kept_as_a_digest_and_one_bit() -> None:
    url = "x.org/s?q=ui+automation+tree"
    before = UrlBefore.of(url, TEXT)
    assert before.carried is True
    assert len(before.digest) == 64
    assert "x.org" not in before.digest
    assert before == UrlBefore.of(url, TEXT)
    assert UrlBefore.of(BEFORE, TEXT).carried is False


def test_om08_a_digest_judges_as_the_url_does() -> None:
    url = "x.org/s?q=ui+automation+tree"
    cases = [(BEFORE, url), (url, url), (url, url + "&page=2"), (BEFORE, BEFORE), (BEFORE, "")]
    for before, after in cases:
        expected = url_query_counts(before, after, TEXT)
        assert UrlBefore.of(before, TEXT).counts(after, TEXT) is expected, (before, after)


def test_om13_the_load_signal_is_a_new_document_named_by_the_words() -> None:
    """#159 review item 1: a title already there before the submit (an omnibox refinement shows
    the pending URL under the old page) or a title that is only the URL's host (Chrome's error
    page) is not a load."""

    def loaded(names: list[str], text: str, before: list[str], url: str) -> bool:
        url_before = UrlBefore.of("", text)
        return page_loaded(
            names, text, before=before, url=url, document_url=None, url_before=url_before
        )

    url = "www.google.com/search?q=google.com"
    assert loaded(["google.com - Google Search"], "google.com", [], url)
    assert not loaded(["google.com"], "google.com", [], url)
    assert not loaded(["www.google.com"], "google.com", [], url)
    old = "ui automation tree - Ecosia"
    refined = "www.ecosia.org/search?q=ui+automation"
    assert not loaded([old], "ui automation", [old], refined)
    assert loaded([old, "ui automation - Ecosia"], "ui automation", [old], refined)


# ---- the load signal with the Document's own URL (probe document_url, Chrome 156, n=5 each) --

MDN_BEFORE = UrlBefore.of("developer.mozilla.org/en-US/", "flexbox")


@pytest.mark.parametrize(
    ("case", "names", "before", "url", "document_url", "loaded"),
    [
        (
            "mdn: title without the query, Document URL with it",
            ["Search | MDN"],
            ["MDN Web Docs"],
            "developer.mozilla.org/en-US/search?q=flexbox",
            "https://developer.mozilla.org/en-US/search?q=flexbox",
            True,
        ),
        (
            "a title holding the words needs no Document URL",
            ["flexbox - Search"],
            ["Home"],
            "x.org/search?q=flexbox",
            None,
            True,
        ),
        (
            "certificate interstitial",
            ["Privacy error"],
            ["MDN Web Docs"],
            "developer.mozilla.org/en-US/search?q=flexbox",
            "chrome-error://chromewebdata/",
            False,
        ),
        (
            "refused port: titled by the host",
            ["developer.mozilla.org"],
            ["MDN Web Docs"],
            "developer.mozilla.org/en-US/search?q=flexbox",
            "chrome-error://chromewebdata/",
            False,
        ),
        (
            "omnibox refinement still pending: the old page",
            ["MDN Web Docs"],
            ["MDN Web Docs"],
            "developer.mozilla.org/en-US/search?q=flexbox",
            "https://developer.mozilla.org/en-US/",
            False,
        ),
        (
            "unreadable Document URL: the words rule only",
            ["Search | MDN"],
            ["MDN Web Docs"],
            "developer.mozilla.org/en-US/search?q=flexbox",
            None,
            False,
        ),
        (
            "a Document URL for another query",
            ["Search | MDN"],
            ["MDN Web Docs"],
            "developer.mozilla.org/en-US/search?q=flexbox",
            "https://developer.mozilla.org/en-US/search?q=grid",
            False,
        ),
        (
            "the pinned gap: a 404 with its own title and the query in its URL counts",
            ["Error response"],
            ["MDN Web Docs"],
            "developer.mozilla.org/missing?q=flexbox",
            "https://developer.mozilla.org/missing?q=flexbox",
            True,
        ),
    ],
)
def test_the_page_loaded_table(  # noqa: PLR0917 - one parametrize row, by name
    case: str,
    names: list[str],
    before: list[str],
    url: str,
    document_url: str | None,
    loaded: bool,
) -> None:
    got = page_loaded(
        names, "flexbox", before=before, url=url, document_url=document_url, url_before=MDN_BEFORE
    )
    assert got is loaded, case
