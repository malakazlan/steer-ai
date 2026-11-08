"""The held-out Phase 1 set (design 12 [v2.43]): ten other single-app tasks, never used to fix
Steer, checked by the same independent reads; a generic search check (results path, query
parameter, words); the inbox page takes the reply text the task approves."""

from __future__ import annotations

import json
from typing import Any

from steerai.probes.present_switch import page_html
from steerai.probes.task_suite import HELD_OUT, TASKS, passed, task_set, verdict


def _task(task_id: str) -> Any:
    return next(t for t in HELD_OUT if t.task_id == task_id)


def _page(href: str, title: str = "") -> str:
    return json.dumps({"href": href, "title": title})


def test_ten_held_out_tasks_none_from_the_development_set() -> None:
    assert len(HELD_OUT) == 10
    assert len({t.task_id for t in HELD_OUT}) == 10
    assert not {t.task_id for t in HELD_OUT} & {t.task_id for t in TASKS}
    assert not {t.goal for t in HELD_OUT} & {t.goal for t in TASKS}


def test_the_set_flag_picks_the_tasks() -> None:
    assert task_set("dev") == TASKS
    assert task_set("held_out") == HELD_OUT


def test_a_search_passes_on_its_path_parameter_and_words() -> None:
    py1 = _task("py1")
    assert passed(py1, "done", _page("https://docs.python.org/3/search.html?q=pathlib"))
    assert not passed(py1, "done", _page("https://docs.python.org/3/library/pathlib.html"))
    assert not passed(py1, "done", _page("https://docs.python.org/3/search.html?q=path"))
    ddg1 = _task("ddg1")
    assert passed(ddg1, "done", _page("https://duckduckgo.com/?q=ui+automation+tree&ia=web"))
    assert not passed(ddg1, "done", _page("https://duckduckgo.com/html/?q=ui+automation+tree"))


def test_the_article_and_the_inbox_checks() -> None:
    assert passed(_task("wk3"), "done", _page("https://en.wikipedia.org/wiki/Grace_Hopper"))
    assert passed(_task("ib3"), "done", "chat-bilal")
    assert passed(_task("ib4"), "done", "chat-sara")
    assert not passed(_task("ib4"), "done", "chat-amir-khan")


def test_the_inbox_page_logs_the_reply_the_task_approves() -> None:
    assert '"see you tomorrow"' in page_html("see you tomorrow")
    assert '"ok thanks"' in page_html()


def test_the_bar_is_judged_over_the_chosen_set() -> None:
    rows = [{"task": t.task_id, "passed": True} for t in HELD_OUT]
    assert verdict(rows, HELD_OUT)[1]
    assert not verdict(rows, TASKS)[1], "dev tasks are absent from a held-out run"


def test_a_results_url_is_built_from_the_task_s_own_check() -> None:
    from steerai.probes.task_suite import results_url  # noqa: PLC0415 - this test only

    assert results_url(_task("py1")) == "https://docs.python.org/3/search.html?q=pathlib"
    assert results_url(_task("ddg1")) == "https://duckduckgo.com/?q=ui+automation+tree"
    url = results_url(_task("bing1"))
    assert passed(_task("bing1"), "done", _page(url)), "a control must pass its own check"
