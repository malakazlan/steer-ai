"""The gallery task set (packet interaction-model-2026-11-12 §5): one goal per actionable row,
each judged on the event the gallery page logs."""

from __future__ import annotations

import json

import pytest

from steerai.probes.gallery_page import LargeTask, large_passes, page_html, parse_large_log
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


# ---- [V2.55] (3): a large page for the listing cap --------------------------------------------


def test_the_large_page_is_served_from_the_gallery_origin() -> None:
    from steerai.probes.gallery_page import LARGE_ROWS, pages  # noqa: PLC0415 - this test only

    served = pages()
    html = served["/large.html"]
    assert "<title>Large inventory</title>" in html
    assert LARGE_ROWS >= 2000
    assert f"var ROWS = {LARGE_ROWS};" in html


def test_the_large_page_has_a_menu_that_reveals_items_and_a_target_far_down() -> None:
    from steerai.probes.gallery_page import LARGE_TARGET, pages  # noqa: PLC0415 - this test only

    html = pages()["/large.html"]
    assert 'aria-haspopup="menu"' in html
    assert 'aria-expanded="false"' in html
    assert 'role="menu" aria-label="Sort by" hidden' in html
    assert LARGE_TARGET >= 1900
    assert f"var TARGET = {LARGE_TARGET};" in html


def test_the_sort_items_are_menuitemradios_that_stay_open_and_reorder_the_rows() -> None:
    """Stage 1 ruling 2026-11-15: "By size" is checked in place (a state proof on the item, so
    the menu stays open after a choice), the rows reorder, and the choice is logged."""
    from steerai.probes.gallery_page import pages  # noqa: PLC0415 - this test only

    html = pages()["/large.html"]
    for key, label in (("name", "By name"), ("date", "By date"), ("size", "By size")):
        item = f'<li role="menuitemradio" aria-checked="false" tabindex="-1" data-key="{key}">'
        assert f"{item}{label}</li>" in html
    assert 'put(e, {row: "large-sort", value: key})' in html
    assert "list.appendChild(sorted[k])" in html
    assert "menu.hidden = true" not in html  # a choice never closes the menu


def test_the_target_is_a_toggle_whose_name_keeps_its_words() -> None:
    """Stage 1 ruling 2026-11-15: the target toggles `aria-pressed` (a state proof) and shows
    "Reserved"; its own name never changes ([V2.40] Q7: a state claim's element must hold the
    param's words; WAI-ARIA APG button pattern: a toggle's label does not change with its
    state)."""
    from steerai.probes.gallery_page import pages  # noqa: PLC0415 - this test only

    html = pages()["/large.html"]
    assert 'button.setAttribute("aria-pressed", "false")' in html
    assert 'button.textContent = "Reserve item " + n' in html
    assert 'mark.textContent = on ? " Reserved" : ""' in html
    assert 'put(e, {row: "large-reserve", item: n, on: on})' in html
    assert 'put(e, {row: "large-details", item: n})' in html  # a stray row is seen


def _entry(row: str, **fields: object) -> dict[str, object]:
    return {"row": row, **fields}


RESERVED = _entry("large-reserve", item=1987, on=True)
OPENED = _entry("large-menu", open=True)
BY_SIZE = _entry("large-sort", value="size")


@pytest.mark.parametrize(
    ("task", "entries", "ok"),
    [
        ("lg1", [RESERVED], True),
        ("lg1", [], False),
        ("lg1", [_entry("large-reserve", item=1986, on=True)], False),
        ("lg1", [RESERVED, _entry("large-reserve", item=1987, on=False)], False),
        ("lg1", [{**RESERVED, "untrusted": True}], False),
        ("lg1", [_entry("large-details", item=4), RESERVED], False),
        ("lg1", [OPENED, RESERVED], False),
        ("lg2", [OPENED, BY_SIZE], True),
        ("lg2", [BY_SIZE], False),
        ("lg2", [OPENED, _entry("large-sort", value="date")], False),
        ("lg2", [OPENED, _entry("large-sort", value="date"), BY_SIZE], False),
        ("lg2", [OPENED, BY_SIZE, RESERVED], False),
        ("lg2", [OPENED, {**BY_SIZE, "untrusted": True}], False),
    ],
)
def test_a_large_task_passes_only_on_its_own_trusted_end_state(
    task: LargeTask, entries: list[dict[str, object]], ok: bool
) -> None:
    assert large_passes(task, parse_large_log(json.dumps(entries))) is ok


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '[{"row": "elsewhere"}]',
        '[{"row": "large-reserve", "item": "1987", "on": true}]',
        '[{"row": "large-reserve", "item": 1987, "on": 1}]',
        '[{"row": "large-sort", "value": 3}]',
        '[{"row": "large-menu", "open": true, "untrusted": "yes"}]',
        "[1]",
    ],
)
def test_a_log_of_another_shape_is_an_error_never_a_pass(raw: str) -> None:
    with pytest.raises(ValueError, match="large log"):
        parse_large_log(raw)


def test_the_large_page_reuses_nothing_from_h7() -> None:
    """[V2.55] (3): generic content, no held-out site's names."""
    from steerai.probes.gallery_page import pages  # noqa: PLC0415 - this test only

    html = pages()["/large.html"].lower()
    for name in ("wikipedia", "youtube", "google", "amazon", "github", "npm"):
        assert name not in html
