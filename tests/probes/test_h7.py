"""H7, the seventh held-out set (V2 §12 step 8, [V2.4]): sealed before any V2 step 3 code,
written without looking at v2 runs; its composition is the lead's ruling."""

from __future__ import annotations

import json

from steerai.probes.h7_page import CITIES, COLOURS, DRAFTS, LOG_KEY, SIZES, TABS, page_html
from steerai.probes.task_suite import (
    GALLERY,
    H2,
    H3,
    H4,
    H5,
    H6,
    H7,
    H7_DRIVES,
    HELD_OUT,
    TASKS,
    task_set,
)

EARLIER = (*TASKS, *HELD_OUT, *H2, *H3, *H4, *H5, *H6, *GALLERY)
NOT_A_SEARCH = {"h7s5", "h7s6", "h7s7"}


def _host(url: str) -> str:
    return url.split("/")[2]


def test_h7_is_twenty_tasks_in_the_ruled_mix() -> None:
    assert task_set("h7") == H7
    assert len(H7) == 20
    widgets = [t for t in H7 if t.start == "h7" and t.task_id.startswith("h7w")]
    commits = [t for t in H7 if t.start == "h7" and t.task_id.startswith("h7c")]
    native = [t for t in H7 if t.start in ("notepad_save", "calculator")]
    sites = [t for t in H7 if t.start.startswith("https://")]
    assert (len(widgets), len(commits), len(native), len(sites)) == (8, 2, 2, 8)
    assert any(t.check == "file_text" for t in native), "a Notepad save read from the file"
    assert {t.task_id for t in sites if not t.goal.startswith("search ")} >= NOT_A_SEARCH


def test_h7_is_new_tasks_and_new_sites_only() -> None:
    ids = {t.task_id for t in EARLIER}
    goals = {t.goal for t in EARLIER}
    hosts = {_host(t.start) for t in EARLIER if t.start.startswith("http")}
    for task in H7:
        assert task.task_id not in ids, task.task_id
        assert task.goal not in goals, task.task_id
        if task.start.startswith("http"):
            assert _host(task.start) not in hosts, task.task_id
    assert len({_host(t.start) for t in H7 if t.start.startswith("http")}) == 8


def test_every_page_task_has_a_control_on_an_element_the_page_has() -> None:
    html = page_html()
    for task in H7:
        if task.start != "h7":
            continue
        drive = H7_DRIVES[task.task_id]
        assert drive, task.task_id
        for kind, arg in drive:
            if kind in ("click", "focus", "type"):
                selector = arg.split("|", 1)[0]
                assert f'id="{selector[1:].split(" ")[0]}"' in html, (task.task_id, arg)


def test_each_page_task_expects_one_event_kind_the_page_logs() -> None:
    html = page_html()
    for task in H7:
        if task.check == "h7_event":
            assert f'kind: "{json.loads(task.expected)["kind"]}"' in html, task.task_id


def test_the_rows_are_the_ruled_idioms() -> None:
    html = page_html()
    assert 'aria-controls="ship-panel"' in html
    assert 'aria-activedescendant="col-crimson"' in html
    assert html.count('role="tab"') == len(TABS)
    assert "attachShadow" in html
    assert 'role="combobox"' in html
    assert 'aria-controls="city-options"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'role="menubar"' in html
    assert 'id="size-menu"' in html
    assert html.count('role="menuitemradio"') == len(SIZES)
    assert 'class="stars"' in html
    assert html.count("data-v=") == 5
    assert html.count('role="option"') == len(COLOURS) + len(CITIES)
    assert html.count('class="draft-delete"') == len(DRAFTS)
    assert 'role="alertdialog"' in html
    assert LOG_KEY == "steer-h7"


def test_the_commit_tasks_approve_only_their_own_words_and_buttons() -> None:
    by_id = {t.task_id: t for t in H7}
    assert by_id["h7c1"].approve == frozenset({"standup moved to 10"})
    assert by_id["h7c1"].approve_targets == frozenset({"Send"})
    assert "Delete" in by_id["h7c2"].approve_targets
    assert by_id["h7n1"].path == "steer-h7.txt"
