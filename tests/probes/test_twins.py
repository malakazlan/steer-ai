"""The #150 fixture probe's pure part: which field's form each search request came from, and
the bar over runs."""

from __future__ import annotations

from steerai.probes.twins import PAGE, submitted, verdict


def test_the_page_has_two_identical_fields_far_apart() -> None:
    assert PAGE.count('id="search-box"') == 2
    assert PAGE.count('aria-label="Quick search"') == 2
    assert 'name="from" value="top"' in PAGE
    assert 'name="from" value="bottom"' in PAGE
    assert PAGE.count("<li><form") == 2, "each field in a list item, as Sphinx renders it"


def test_submitted_reads_the_form_and_the_query_of_each_search() -> None:
    paths = [
        "/page.html",
        "/search?q=pathlib&from=top",
        "/favicon.ico",
        "/search?from=bottom&q=a+b",
    ]
    assert submitted(paths) == [("top", "pathlib"), ("bottom", "a b")]


def test_the_bar_is_every_run_done_with_one_search_from_the_top_field() -> None:
    good = {"status": "done", "searches": [["top", "pathlib"]]}
    assert verdict([good] * 5) == (5, True)
    assert verdict([good, {"status": "done", "searches": [["bottom", "pathlib"]]}]) == (1, False)
    assert verdict([good, {"status": "stopped", "searches": []}]) == (1, False)
    assert verdict([good, {"status": "done", "searches": [["top", "pathlib"]] * 2}]) == (1, False)
    assert verdict([]) == (0, False)
