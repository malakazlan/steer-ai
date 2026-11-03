"""Claude-backed element chooser: same contract as the Jev chooser, no network in unit tests."""

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError

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


# --- boundary values -------------------------------------------------------------------------


def test_probabilities_that_sum_above_one_are_renormalised() -> None:
    client = _FakeParse([_ranking(("3", 0.5), ("2", 0.5), ("4", 0.5))])
    answer = ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=LINES)
    assert sum(answer.probabilities.values()) == pytest.approx(1.0)
    assert {k: answer.probabilities[k] for k in ("2", "3", "4")} == pytest.approx(
        {"2": 1 / 3, "3": 1 / 3, "4": 1 / 3}
    )
    assert answer.probabilities["1"] == 0.0
    assert answer.probabilities["none"] == 0.0
    # peakedness over all 5 options (4 ids + none), as Jev defines confidence
    assert answer.confidence == pytest.approx((5 * (1 / 3) - 1) / 4)
    assert answer.margin == pytest.approx(0.0)


def test_probabilities_that_sum_below_one_keep_the_residual_as_none() -> None:
    # The model assigned 30 % to listed ids; the other 70 % is "nothing fits" (edge case CC-06).
    client = _FakeParse([_ranking(("3", 0.2), ("2", 0.1))])
    answer = ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=LINES)
    assert answer.probabilities["none"] == pytest.approx(0.7)
    assert answer.probabilities["3"] == pytest.approx(0.2)
    assert answer.element_id is None


def test_zero_probability_entries_are_dropped() -> None:
    client = _FakeParse([_ranking(("3", 1.0), ("2", 0.0), ("none", 0.0))])
    answer = ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=LINES)
    assert answer.probabilities["3"] == 1.0
    assert all(answer.probabilities[k] == 0.0 for k in ("1", "2", "4", "none"))
    assert answer.element_id == 3
    assert answer.confidence == 1.0
    assert answer.margin == pytest.approx(1.0)


def test_all_zero_probabilities_is_an_error() -> None:
    client = _FakeParse([_ranking(("3", 0.0), ("2", 0.0))])
    with pytest.raises(ValueError, match="empty ranking after dropping"):
        ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=LINES)


def test_all_unknown_ids_is_an_error() -> None:
    client = _FakeParse([_ranking(("98", 0.6), ("99", 0.4))])
    with pytest.raises(ValueError, match="empty ranking after dropping unknown ids"):
        ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=LINES)


def test_one_all_unknown_vote_among_valid_votes_fails_closed() -> None:
    client = _FakeParse([_ranking(("3", 1.0)), _ranking(("99", 1.0)), _ranking(("3", 1.0))])
    with pytest.raises(ValueError, match="empty ranking after dropping unknown ids"):
        ClaudeChooser(parse=client, model="m", votes=3).choose_element(intent="x", lines=LINES)


def test_duplicate_ids_in_a_ranking_do_not_flip_the_top_candidate() -> None:
    # The model listed 3 first at 0.6, repeated 3 at 0.1, then 2 at 0.3. Whatever merge rule is
    # chosen, 3 carries the most mass and must win.
    client = _FakeParse([_ranking(("3", 0.6), ("3", 0.1), ("2", 0.3))])
    answer = ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=LINES)
    assert sum(answer.probabilities.values()) == pytest.approx(1.0)
    assert answer.element_id == 3


def test_whitespace_padded_ids_match_their_element() -> None:
    client = _FakeParse([_ranking((" 3", 0.6), ("2 ", 0.4))])
    answer = ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=LINES)
    assert answer.element_id == 3
    assert (answer.probabilities["3"], answer.probabilities["2"]) == pytest.approx((0.6, 0.4))


def test_whitespace_padded_none_maps_to_no_element() -> None:
    client = _FakeParse([_ranking(("none ", 1.0))])
    answer = ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=LINES)
    assert answer.element_id is None


def test_tie_between_none_and_an_element_goes_to_the_first_option() -> None:
    # Ties resolve to the first option in list order (element ids first, `none` last), never by
    # the model's listing order; margin 0 means the orchestrator must escalate anyway.
    none_first = _FakeParse([_ranking(("none", 0.5), ("3", 0.5))])
    answer = ClaudeChooser(parse=none_first, model="m").choose_element(intent="x", lines=LINES)
    assert answer.element_id == 3
    assert answer.margin == 0.0
    assert answer.confidence == pytest.approx((5 * 0.5 - 1) / 4)
    element_first = _FakeParse([_ranking(("3", 0.5), ("none", 0.5))])
    answer = ClaudeChooser(parse=element_first, model="m").choose_element(intent="x", lines=LINES)
    assert answer.element_id == 3


def test_empty_screen_makes_no_call_and_returns_none() -> None:
    # Edge case CC-13: nothing to choose from, so no hosted call is spent.
    client = _FakeParse([_ranking(("1", 1.0))])
    answer = ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=[])
    assert answer.element_id is None
    assert answer.probabilities == {"none": 1.0}
    assert client.calls == []


def test_truncation_trailer_is_not_a_valid_id() -> None:
    lines = [*LINES, "(... 12 more below)"]
    client = _FakeParse([_ranking(("(...", 1.0))])
    with pytest.raises(ValueError, match="empty ranking after dropping unknown ids"):
        ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=lines)


def test_non_ranking_parsed_output_is_an_error() -> None:
    @dataclass
    class _Bad:
        parsed_output: None = None
        model: str = "m"

    with pytest.raises(ValueError, match="empty ranking from the model"):
        ClaudeChooser(parse=lambda **_: _Bad(), model="m").choose_element(intent="x", lines=LINES)


def test_model_reported_is_the_responders_not_the_requested() -> None:
    class _Renamed(_FakeParse):
        def __call__(self, **kwargs: Any) -> _FakeParsed:
            parsed = super().__call__(**kwargs)
            return _FakeParsed(parsed_output=parsed.parsed_output, model="claude-opus-5-20261001")

    chooser = ClaudeChooser(parse=_Renamed([_ranking(("3", 1.0))]), model="claude-opus-5")
    answer = chooser.choose_element(intent="x", lines=LINES)
    assert answer.model == "claude-opus-5-20261001"


@pytest.mark.parametrize("votes", [0, -1])
def test_votes_below_one_rejected(votes: int) -> None:
    with pytest.raises(ValueError, match="votes must be between 1 and 5"):
        ClaudeChooser(parse=_FakeParse([]), model="m", votes=votes)


def test_votes_of_one_makes_exactly_one_call() -> None:
    client = _FakeParse([_ranking(("3", 1.0))])
    ClaudeChooser(parse=client, model="m", votes=1).choose_element(intent="x", lines=LINES)
    assert len(client.calls) == 1


@pytest.mark.parametrize("p", [-0.1, 1.1])
def test_probability_outside_unit_interval_rejected_by_schema(p: float) -> None:
    with pytest.raises(ValidationError):
        RankedChoice(id="3", p=p)


def test_from_env_reads_model_and_votes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("STEER_CLAUDE_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("STEER_CLAUDE_VOTES", "3")
    chooser = ClaudeChooser.from_env()
    assert chooser._model == "claude-sonnet-5"
    assert chooser._votes == 3


def test_from_env_empty_strings_use_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("STEER_CLAUDE_MODEL", "")
    monkeypatch.setenv("STEER_CLAUDE_VOTES", "")
    chooser = ClaudeChooser.from_env()
    assert chooser._model == "claude-opus-5"
    assert chooser._votes == 1


@pytest.mark.parametrize("raw", ["0", "-2"])
def test_from_env_votes_below_one_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("STEER_CLAUDE_VOTES", raw)
    with pytest.raises(ValueError, match="votes must be between 1 and 5"):
        ClaudeChooser.from_env()


def test_empty_intent_is_rejected_before_any_call() -> None:
    client = _FakeParse([_ranking(("3", 1.0))])
    with pytest.raises(ValueError, match="intent must not be empty"):
        ClaudeChooser(parse=client, model="m").choose_element(intent="   ", lines=LINES)
    assert client.calls == []


def test_duplicate_ids_are_summed_not_overwritten() -> None:
    client = _FakeParse([_ranking(("3", 0.6), ("3", 0.1), ("2", 0.3))])
    answer = ClaudeChooser(parse=client, model="m").choose_element(intent="x", lines=LINES)
    assert answer.probabilities["3"] == pytest.approx(0.7)
    assert answer.probabilities["2"] == pytest.approx(0.3)


def test_votes_add_a_distinct_nonce_per_call() -> None:
    client = _FakeParse([_ranking(("3", 1.0)), _ranking(("3", 1.0))])
    ClaudeChooser(parse=client, model="m", votes=2).choose_element(intent="x", lines=LINES)
    bodies = [c["messages"][0]["content"] for c in client.calls]
    assert bodies[0] != bodies[1]
    assert all("request: " in b for b in bodies)


def test_votes_above_the_cap_rejected() -> None:
    with pytest.raises(ValueError, match="votes must be between 1 and 5"):
        ClaudeChooser(parse=_FakeParse([]), model="m", votes=6)


def test_from_env_non_integer_votes_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("STEER_CLAUDE_VOTES", "abc")
    with pytest.raises(ValueError, match="STEER_CLAUDE_VOTES"):
        ClaudeChooser.from_env()


def test_ranking_schema_caps_candidates_at_five() -> None:
    with pytest.raises(ValidationError):
        Ranking(ranked=[RankedChoice(id=str(i), p=0.1) for i in range(6)])
