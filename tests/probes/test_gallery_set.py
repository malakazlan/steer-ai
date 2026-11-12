"""The gallery task set (packet interaction-model-2026-11-12 §5): one goal per actionable row,
each judged on the event the gallery page logs."""

from __future__ import annotations

import json

from steerai.probes.gallery_page import page_html
from steerai.probes.task_suite import GALLERY, h2_event, passed, task_set


def test_the_gallery_set_is_its_rows() -> None:
    assert task_set("gallery") == GALLERY
    assert [t.task_id for t in GALLERY][:3] == ["g01", "g02", "g03"]
    assert len({t.task_id for t in GALLERY}) == len(GALLERY)


def test_every_goal_expects_a_row_the_page_logs() -> None:
    html = page_html()
    for task in GALLERY:
        row = json.loads(task.expected)["row"]
        assert f'{{row: "{row}"' in html or f'"{row}"' in html, task.task_id


def test_a_row_passes_only_on_its_own_trusted_event() -> None:
    g03 = next(t for t in GALLERY if t.task_id == "g03")

    def judged(entries: list[dict[str, object]]) -> bool:
        return passed(g03, "done", h2_event(json.dumps(entries)))

    assert judged([{"row": "g03", "on": True}]) is True
    assert judged([{"row": "g03", "on": False}]) is False
    assert judged([{"row": "g02", "on": True}]) is False
    assert judged([{"row": "g03", "on": True, "untrusted": True}]) is False


def test_the_typed_row_logs_its_last_input_and_the_submit_row_approves_its_words() -> None:
    """Gallery g22 logged `change`, which fires on blur (Steer typed and the row saw nothing);
    g23's R2 Enter asked and the scripted person had no approved text."""
    html = page_html()
    assert 'on("g22", "input"' in html
    assert "keep only the last" in html
    g23 = next(t for t in GALLERY if t.task_id == "g23")
    assert g23.approve == frozenset({"automation"})


def test_the_link_row_opens_a_page_titled_like_the_link() -> None:
    """Gallery g25 (0/3): the link cancelled its navigation and set a status line no planner can
    predict; a "Terms of service" link opens a page with that title."""
    from steerai.probes.gallery_page import pages  # noqa: PLC0415 - this test only

    served = pages()
    assert 'href="/terms.html"' in served["/page.html"]
    assert "<title>Terms of service</title>" in served["/terms.html"]
    assert 'preventDefault(); say("Terms opened")' not in served["/page.html"]


def test_the_search_row_submits_a_form_to_a_results_page_titled_with_the_words() -> None:
    """Gallery g23 (0/3 after the approval fix): the box only wrote a status line, so no proof
    of the R2 submit could hold; a real search box submits to a results page."""
    from steerai.probes.gallery_page import pages  # noqa: PLC0415 - this test only

    served = pages()
    assert '<form action="/search.html" method="get"' in served["/page.html"]
    assert 'name="q"' in served["/page.html"]
    assert 'document.title = q + " - Gallery search"' in served["/search.html"]
