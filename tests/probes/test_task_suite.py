"""The Phase 1 suite's pure parts: ten tasks, the person who approves only the goal's words,
the independent checks, and the bar."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from steerai.probes.task_suite import TASKS, Person, passed, query_of, verdict


def _task(task_id: str) -> Any:
    return next(t for t in TASKS if t.task_id == task_id)


def test_ten_distinct_tasks() -> None:
    assert len(TASKS) == 10
    assert len({t.task_id for t in TASKS}) == 10


def test_the_person_approves_only_the_goal_s_own_words() -> None:
    person = Person("search Google for windows ui automation")

    def ask(text: str | None, kind: str = "yes") -> Any:
        return SimpleNamespace(
            decision_id="d1", params={"text": text}, verb="submit", risk="R2", kind=kind
        )

    assert person.ask(ask("windows ui automation")) == "yes:d1"
    assert person.ask(ask("my password is x")) == "no:d1"
    assert person.ask(ask(None)) == "no:d1"
    assert person.ask(ask("windows ui automation", kind="which")) == "no:d1"
    assert person.ask_go("go:1") == "go"


def test_query_of_reads_google_and_youtube() -> None:
    assert query_of("https://www.google.com/search?q=windows+ui+automation&hl=en") == (
        "windows ui automation"
    )
    assert query_of("https://www.youtube.com/results?search_query=lofi+hip+hop") == "lofi hip hop"
    assert query_of("https://www.youtube.com/") == ""


def test_a_pass_needs_done_and_the_independent_read() -> None:
    assert passed(_task("np1"), "done", "hello from steer")
    assert not passed(_task("np1"), "done", "hello from stee")
    assert not passed(_task("np1"), "stopped", "hello from steer")
    assert passed(_task("g2"), "done", "https://www.google.com/search?q=Windows+UI+Automation")
    assert not passed(_task("g2"), "done", "https://www.google.com/?hl=en")
    assert passed(_task("yt2"), "done", "https://www.youtube.com/watch?v=abc")
    assert not passed(_task("yt2"), "done", "https://www.youtube.com/results?search_query=x")
    assert passed(_task("wk1"), "done", "https://en.wikipedia.org/wiki/Alan_Turing")
    assert passed(_task("ib1"), "done", "chat-amir-khan")
    assert not passed(_task("ib1"), "done", "sends:2")


def test_the_bar_needs_every_task_and_every_run() -> None:
    rows = [{"task": t.task_id, "passed": True} for t in TASKS]
    assert verdict(rows)[1]
    rows[3] = {"task": rows[3]["task"], "passed": False, "invalid": "not_in_front"}
    lines, ok = verdict(rows)
    assert not ok
    assert "(1 invalid)" in lines[3]
    assert not verdict(rows[:9])[1]
    assert not verdict([])[1]
