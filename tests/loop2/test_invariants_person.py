"""[V2.15] Q11: the suite's scripted yes for v2 asks. A plain-field search on a real site may
dispatch after a scripted yes only when the subgoal's commit is `none`, the text is a goal span,
and the field and the text are named in the request. Red on the step 4d stub."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.probes.v2_person import AskFacts, scripted_yes, shares_content_word, unnamed_count
from tests.loop2.world_fakes import GOAL, pending

SEARCH = AskFacts(subgoal_commit="none", text_from="request", field_name="Search", text="red shoes")


@pending("[V2.15] Q11: every condition holds, so the scripted person says yes")
def test_a_search_named_in_the_request_with_its_words_gets_a_yes() -> None:
    assert scripted_yes(GOAL, SEARCH) is True


@pending("[V2.15] Q11: the subgoal's commit must be none")
def test_a_commit_subgoal_never_gets_the_scripted_yes() -> None:
    assert scripted_yes(GOAL, dataclasses.replace(SEARCH, subgoal_commit="send")) is False


@pending("[V2.15] Q11: the text must be a goal span")
def test_generated_text_never_gets_the_scripted_yes() -> None:
    assert scripted_yes(GOAL, dataclasses.replace(SEARCH, text_from="agent")) is False


@pending("[V2.15] Q11: the text must be named in the request")
def test_text_not_in_the_request_never_gets_the_scripted_yes() -> None:
    assert scripted_yes(GOAL, dataclasses.replace(SEARCH, text="blue boots")) is False


@pending("[V2.15] Q11: the field must be named in the request")
def test_a_field_not_named_in_the_request_never_gets_the_scripted_yes() -> None:
    assert scripted_yes(GOAL, dataclasses.replace(SEARCH, field_name="Message")) is False


@pending("[V2.15] Q11: an ask with no text is not a search")
def test_an_ask_without_text_never_gets_the_scripted_yes() -> None:
    assert scripted_yes(GOAL, dataclasses.replace(SEARCH, text=None, text_from=None)) is False


@pending("[V2.17].3: a stop word is never a shared content word")
def test_for_does_not_name_a_comment_field() -> None:
    assert shares_content_word("Comment for the author", GOAL) is False


@pending("[V2.17].3: a content word of three letters or more names the field")
def test_a_shared_content_word_names_the_field() -> None:
    assert shares_content_word("Search products", GOAL) is True


@pending("[V2.17].3: whole tokens only")
def test_a_word_inside_a_longer_word_does_not_name_the_field() -> None:
    assert shares_content_word("Shopping list", GOAL) is False


@pending("[V2.17].3: the text equals a goal span exactly")
@pytest.mark.parametrize("text", ["Red Shoes", "red  shoes", "red shoes "])
def test_text_that_is_not_exactly_a_goal_span_never_gets_the_scripted_yes(text: str) -> None:
    assert scripted_yes(GOAL, dataclasses.replace(SEARCH, text=text)) is False


@pending("[V2.17].3: the unnamed-field declines are counted and reported")
def test_unnamed_field_declines_are_counted() -> None:
    unnamed = dataclasses.replace(SEARCH, field_name="Comment for the author")
    generated = dataclasses.replace(SEARCH, text_from="agent")
    assert unnamed_count(GOAL, [SEARCH, unnamed, generated]) == 1
