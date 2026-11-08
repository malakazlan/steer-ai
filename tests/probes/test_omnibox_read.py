"""The omnibox probe's pure part: the words a query parameter carries in an omnibox value."""

from __future__ import annotations

from steerai.probes.omnibox_read import query_words


def test_query_words_decode_from_a_scheme_less_value() -> None:
    value = "127.0.0.1:59899/results?q=ui+automation+tree"
    assert query_words(value, "q") == "ui automation tree"
    assert query_words("search.yahoo.com/search?p=a%20b", "p") == "a b"
    assert query_words("https://www.mojeek.com/search?q=caf%C3%A9", "q") == "café"
    assert query_words("www.ecosia.org/search?q=x", "p") == ""


def test_query_words_keep_case() -> None:
    assert query_words("x.org/s?q=UI+Tree", "q") == "UI Tree"
