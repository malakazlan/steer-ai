"""H4, the fourth held-out set (packet h4-2026-11-10): new tasks only, every page task's control
drives an element the page has, every check is well formed, the titles are the measured ones."""

from __future__ import annotations

import json
import re
from typing import Any

from steerai.probes.h4_page import LANGUAGES, LOG_KEY, POLICY_TEXT, page_html
from steerai.probes.task_suite import (
    H2,
    H3,
    H4,
    H4_DRIVES,
    HELD_OUT,
    TASKS,
    passed,
    results_url,
    task_set,
)

TITLES = {
    "dr1": "Releases - Docs.rs",
    "deb1": "Debian -- Package Search Results -- curl",
    "gut1": "Books: dickens - Project Gutenberg",
    "rg1": "RubyGems.org | your community gem host",
}
"""The results titles measured for the packet (2026-11-10, n=2)."""


def _task(task_id: str) -> Any:
    return next(t for t in H4 if t.task_id == task_id)


def test_h4_is_new_tasks_only() -> None:
    assert len(H4) == 10
    ids = [task.task_id for task in H4]
    assert len(ids) == len(set(ids))
    earlier = [*TASKS, *HELD_OUT, *H2, *H3]
    assert not {task.task_id for task in earlier} & set(ids)
    assert not {task.goal for task in earlier} & {task.goal for task in H4}
    assert task_set("h4") == H4


def test_every_page_task_has_a_control_on_an_element_the_page_has() -> None:
    html = page_html()
    page_ids = set(re.findall(r'id="([^"]+)"', html))
    page_tasks = [task for task in H4 if task.start == "h4"]
    assert {task.task_id for task in page_tasks} == set(H4_DRIVES) == {"q1", "q2", "q3", "q4", "q5"}
    for task_id, drive in H4_DRIVES.items():
        for kind, arg in drive:
            if kind in ("click", "focus"):
                assert arg.startswith("#"), (task_id, arg)
                assert arg[1:] in page_ids, (task_id, arg)


def test_each_page_task_expects_one_event_kind_the_page_logs() -> None:
    html = page_html()
    assert LOG_KEY in html
    assert "isTrusted" in html
    for task in H4:
        if task.check == "h4_event":
            event = json.loads(task.expected)
            assert f'kind: "{event["kind"]}"' in html, task.task_id
    q3 = json.loads(next(t.expected for t in H4 if t.task_id == "q3"))
    assert q3["kinds"] == ["city"]
    assert 'kind: "city"' in html


def test_the_shapes_use_the_packet_idioms() -> None:
    html = page_html()
    assert "<details" in html, "q1 is a native disclosure"
    assert "<summary" in html
    assert "aria-expanded" not in html, "q1 is not the H3 accordion button"
    assert POLICY_TEXT in html
    assert html.count(POLICY_TEXT) == 1, "the panel's first line is unique"
    assert '<select id="lang" size="8"' in html, "q2 is a native list select"
    assert 'role="listbox"' not in html, "q2 is not the H3 ARIA listbox"
    assert '<ul role="tablist"' in html, "q4 tabs are anchors in a list"
    assert '<a role="tab"' in html
    assert '<button role="tab"' not in html
    assert "<fieldset><legend>Sign-up</legend>" in html
    assert "I agree to the terms" in html


def test_swahili_is_scrolled_out_of_the_eight_visible_rows() -> None:
    assert len(LANGUAGES) == 24
    assert len(set(LANGUAGES)) == 24
    assert "Swahili" in LANGUAGES
    assert LANGUAGES.index("Swahili") >= 16, "a size-8 box shows eight rows at a time"
    assert [name for name in LANGUAGES if name.startswith("S")] == ["Swahili"]


def test_the_search_tasks_check_their_measured_titles() -> None:
    searches = [task for task in H4 if task.check == "url_search"]
    assert {task.task_id for task in searches} == set(TITLES)
    for task in searches:
        assert task.start.startswith("https://")
        assert task.path.startswith("/")
        assert task.param
        assert task.title == TITLES[task.task_id], task.task_id


def test_a_results_page_with_another_title_fails() -> None:
    dr1 = _task("dr1")
    href = "https://docs.rs/releases/search?query=serde"
    assert passed(dr1, "done", json.dumps({"href": href, "title": "Releases - Docs.rs"}))
    assert not passed(dr1, "done", json.dumps({"href": href, "title": "Docs.rs"}))
    other = "https://docs.rs/search?query=serde"
    assert not passed(dr1, "done", json.dumps({"href": other, "title": "Releases - Docs.rs"}))
    assert results_url(dr1) == href


def test_the_arch_wiki_task_is_judged_on_the_article_path() -> None:
    aw1 = _task("aw1")
    assert aw1.check == "wiki_article"
    assert aw1.expected == "/title/Pacman"
    assert aw1.approve == frozenset({"pacman"})
    article = "https://wiki.archlinux.org/title/Pacman"
    assert passed(aw1, "done", json.dumps({"href": article, "title": "pacman - ArchWiki"}))
    search = "https://wiki.archlinux.org/index.php?search=pacman&title=Special%3ASearch"
    assert not passed(aw1, "done", json.dumps({"href": search, "title": "Search results"}))
    tips = "https://wiki.archlinux.org/title/Pacman/Tips_and_tricks"
    assert not passed(aw1, "done", json.dumps({"href": tips, "title": "pacman/Tips - ArchWiki"}))
    assert results_url(aw1) == article


def test_q3_is_judged_on_the_final_state() -> None:
    q3 = _task("q3")
    assert q3.check == "h4_final"
    state = {"city": "Osaka", "country": "Japan"}
    done = json.dumps({"kinds": ["city"], "state": state, "untrusted": False})
    assert passed(q3, "done", done)
    scripted = json.dumps({"kinds": ["city"], "state": state, "untrusted": True})
    assert not passed(q3, "done", scripted)
    wrong = json.dumps(
        {"kinds": ["city"], "state": {"city": "Osaka", "country": "Germany"}, "untrusted": False}
    )
    assert not passed(q3, "done", wrong)
    assert not passed(q3, None, done)


def test_q5_names_the_consent_box_in_its_goal_for_the_harness_rule() -> None:
    q5 = _task("q5")
    assert "i agree to the terms" in q5.goal.casefold()
    assert not q5.approve_targets, "the goal rule approves it, not a listed name"


def test_the_near_copies_are_out() -> None:
    assert not [task for task in H4 if task.start in ("calculator", "notepad", "inbox")]
    assert "aria-expanded" not in page_html()
