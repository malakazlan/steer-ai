"""Claude-backed element chooser: same contract as the Jev chooser, no network in unit tests."""

from dataclasses import dataclass
from typing import Any

import pytest

from steerai.claude_choice import ClaudeChooser, RankedChoice, Ranking, build_prompt
from steerai.decision import ElementChooser
from steerai.jev import ElementChoice

LINES = [
    '1 Window "Inbox" [0,0,800,600]',
    '2   ListItem "Amir · hi bro · 2h" [20,140,300,190]',
    '3   ListItem "Amir Khan · ok · 3d" [20,190,300,240]',
    '4   Button "New message" [320,60,420,80]',
]


@dataclass
class _FakeParsed:
    parsed_output: Ranking
    model: str = "claude-opus-5"


class _FakeParse:
    """Stands in for the bound `messages.parse` method."""

    def __init__(self, rankings: list[Ranking]) -> None:
        self._rankings = list(rankings)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _FakeParsed:
        self.calls.append(kwargs)
        return _FakeParsed(parsed_output=self._rankings.pop(0))


def _ranking(*pairs: tuple[str, float]) -> Ranking:
    return Ranking(ranked=[RankedChoice(id=i, p=p) for i, p in pairs])


def test_build_prompt_includes_intent_screen_and_none_rule() -> None:
    system, user = build_prompt(intent="open the chat with Amir Khan", lines=LINES)
    assert "none" in system
    assert "open the chat with Amir Khan" in user
    assert LINES[2] in user


def test_chooser_satisfies_the_shared_protocol() -> None:
    chooser: ElementChooser = ClaudeChooser(parse=_FakeParse([]), model="claude-opus-5")
    assert hasattr(chooser, "choose_element")


def test_choose_element_maps_ranking_to_element_choice() -> None:
    client = _FakeParse([_ranking(("3", 0.8), ("2", 0.14), ("none", 0.06))])
    answer = ClaudeChooser(parse=client, model="claude-opus-5").choose_element(
        intent="open the chat with Amir Khan", lines=LINES
    )
    assert isinstance(answer, ElementChoice)
    assert answer.element_id == 3
    assert answer.margin == pytest.approx(0.8 - 0.14)
    assert answer.probabilities["3"] == pytest.approx(0.8)
    assert 0.0 < answer.confidence <= 1.0
    assert answer.model == "claude-opus-5"
    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_format"] is Ranking


def test_unknown_ids_are_dropped_and_probabilities_renormalised() -> None:
    client = _FakeParse([_ranking(("3", 0.6), ("99", 0.3), ("none", 0.1))])
    chooser = ClaudeChooser(parse=client, model="claude-opus-5")
    answer = chooser.choose_element(intent="x", lines=LINES)
    assert "99" not in answer.probabilities
    assert sum(answer.probabilities.values()) == pytest.approx(1.0)
    assert answer.element_id == 3


def test_none_wins_maps_to_no_element() -> None:
    client = _FakeParse([_ranking(("none", 0.7), ("4", 0.3))])
    chooser = ClaudeChooser(parse=client, model="claude-opus-5")
    answer = chooser.choose_element(intent="x", lines=LINES)
    assert answer.element_id is None


def test_votes_average_distributions_across_calls() -> None:
    client = _FakeParse(
        [_ranking(("3", 1.0)), _ranking(("2", 1.0)), _ranking(("3", 1.0))],
    )
    answer = ClaudeChooser(parse=client, model="claude-opus-5", votes=3).choose_element(
        intent="x", lines=LINES
    )
    assert len(client.calls) == 3
    assert answer.element_id == 3
    assert answer.probabilities["3"] == pytest.approx(2 / 3)
    assert answer.probabilities["2"] == pytest.approx(1 / 3)


def test_empty_ranking_is_an_error() -> None:
    client = _FakeParse([Ranking(ranked=[])])
    with pytest.raises(ValueError, match="empty ranking"):
        ClaudeChooser(parse=client, model="claude-opus-5").choose_element(intent="x", lines=LINES)
