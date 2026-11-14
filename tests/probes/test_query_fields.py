"""[V2.14] decision 2: the data-only query-class analysis. One candidate rule, scored against the
commit-class search fields (positives) and every non-search field on the local pages (negatives).
No rule is merged into the classifier; the second number must be 0."""

from __future__ import annotations

from steerai.probes.query_fields import (
    COMMIT_WORDS,
    NEGATIVE_PAGES,
    FieldFacts,
    candidate,
    evaluate,
    negative_facts,
)


def field(**kw: object) -> FieldFacts:
    base: dict[str, object] = {
        "source": "positive",
        "task": "t1",
        "aria_role": "textbox",
        "name": "Search packages",
        "automation_id": "",
        "multiline": False,
        "group_name": "",
        "buttons": (),
    }
    base.update(kw)
    return FieldFacts(**base)  # type: ignore[arg-type]  # test kwargs, typed by the dataclass


def test_a_single_line_field_named_for_search_passes() -> None:
    assert candidate(field()) == (True, "search_word")


def test_a_search_word_in_the_automation_id_counts_camel_split() -> None:
    assert candidate(field(name="", automation_id="siteSearchInput"))[0] is True


def test_a_search_word_in_the_group_name_counts() -> None:
    assert candidate(field(name="Type here", group_name="Site search"))[0] is True


def test_no_search_word_fails() -> None:
    assert candidate(field(name="Type here")) == (False, "no_search_word")


def test_a_multi_line_field_fails_even_with_a_search_word() -> None:
    assert candidate(field(multiline=True)) == (False, "multiline")


def test_an_unread_multi_line_fact_fails_closed() -> None:
    assert candidate(field(multiline=None)) == (False, "multiline_unread")


def test_a_commit_word_on_the_field_fails() -> None:
    assert candidate(field(name="Search and send"))[1] == "commit_word"


def test_a_commit_word_on_a_button_in_the_group_fails() -> None:
    assert candidate(field(buttons=("Search", "Post")))[1] == "commit_word"


def test_a_message_composer_never_passes() -> None:
    composer = field(
        source="negative", name="Message", multiline=True, group_name="Chat", buttons=("Send",)
    )
    assert candidate(composer)[0] is False


def test_evaluate_counts_recovered_positives_and_false_accepts() -> None:
    rows = [
        field(task="p1"),
        field(task="p2", name="Type here"),
        field(task="n1", source="negative", name="Find a contact"),
        field(task="n2", source="negative", name="Comment", multiline=True),
    ]
    report = evaluate(rows)
    assert report["positives"] == 2
    assert report["recovered"] == ["p1"]
    assert report["negatives"] == 2
    assert report["false_accepts"] == ["n1"]


def test_each_search_worded_negative_page_builds_and_holds_its_one_field() -> None:
    assert sorted(NEGATIVE_PAGES) == ["sw1", "sw2", "sw3"]
    names = {task: [f.name for f in negative_facts(task)] for task in NEGATIVE_PAGES}
    assert names == {
        "sw1": ["Search or add a task"],
        "sw2": ["Search notes"],
        "sw3": ["Reply (find in thread)"],
    }
    assert all(f.source == "negative" for t in NEGATIVE_PAGES for f in negative_facts(t))


def test_the_search_notes_field_sits_beside_a_delete_button() -> None:
    (notes,) = negative_facts("sw2")
    assert notes.buttons == ("Delete",)


def test_the_add_a_task_field_has_no_commit_word_nearby() -> None:
    (task_field,) = negative_facts("sw1")
    assert task_field.buttons == ()
    assert not any(w in task_field.name.lower() for w in COMMIT_WORDS)


def test_the_search_worded_negatives_expose_the_rule_where_it_accepts() -> None:
    """The candidate rule accepts the add-a-task field (the expected failure, V2.28 item 4) and
    rejects the other two on a commit word."""
    outcome = {t: candidate(f)[0] for t in sorted(NEGATIVE_PAGES) for f in negative_facts(t)}
    assert outcome == {"sw1": True, "sw2": False, "sw3": False}
    assert evaluate([f for t in NEGATIVE_PAGES for f in negative_facts(t)])["false_accepts"] == [
        "sw1"
    ]
