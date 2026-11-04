"""The LLM planner, slice 1 (design 5.3 [v1.8]; packet planner-2026-11-04): a goal and the
screen text in, a validated 6.3 plan out, through the one plan boundary. The SDK is a fake
`parse` here; the real call is behind the `live` marker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx2 as httpx
import pytest
from pydantic import ValidationError

from steerai.errors import ErrorCode
from steerai.planner.claude_planner import (
    DEFAULT_MODEL,
    PROMPT_VERSION,
    ClaudePlanner,
    build_prompt,
)
from steerai.planner.contracts import PlannerError, PlanRequest
from steerai.planner.mirror import (
    Decoration,
    ParamsDoc,
    PlanDoc,
    PredicateDoc,
    StepDoc,
    SubgoalDoc,
    TargetDoc,
)

GOAL = "search Google for steer ai computer use agent"
QUERY = "steer ai computer use agent"
SCREEN = (
    '1  Document "Google"                      [0,0,1100,760]',
    '2    ComboBox "Search"                    [84,399,1392,554]',
    '3    Button "I\'m Feeling Lucky"            [600,600,700,640]',
)


def _doc() -> PlanDoc:
    condition = PredicateDoc(
        kind="appeared",
        role="Document",
        name=QUERY,
        decoration=Decoration.google_search,
        replaces=True,
    )
    return PlanDoc(
        goal=GOAL,
        subgoals=[SubgoalDoc(id="g1", desc="results shown", condition=condition)],
        steps=[
            StepDoc(
                id="focus",
                subgoal="g1",
                verb="focus",
                target=TargetDoc(role="ComboBox", name="Search"),
                risk="R0",
            ),
            StepDoc(
                id="type",
                subgoal="g1",
                verb="type",
                target=TargetDoc(role="ComboBox", name="Search"),
                risk="R1",
                text=QUERY,
            ),
            StepDoc(
                id="submit",
                subgoal="g1",
                verb="submit",
                target=TargetDoc(role="ComboBox", name="Search"),
                risk="R1",
                params=ParamsDoc(text=QUERY),
                expect=condition,
            ),
        ],
    )


@dataclass
class _Usage:
    input_tokens: int = 1200
    output_tokens: int = 300


@dataclass
class _Response:
    parsed_output: Any
    model: str = "claude-opus-5-20260301"
    stop_reason: str | None = "end_turn"
    usage: _Usage | None = None


class FakeParse:
    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _request(goal: str = GOAL) -> PlanRequest:
    return PlanRequest(goal=goal, screen_lines=SCREEN, window_title="Google - Google Chrome")


def _planner(response: _Response | Exception) -> tuple[ClaudePlanner, FakeParse]:
    parse = FakeParse(response)
    return ClaudePlanner(parse=parse), parse


# ---- the happy path ----------------------------------------------------------------------------


def test_a_valid_document_becomes_a_plan_through_the_file_boundary() -> None:
    planner, parse = _planner(_Response(parsed_output=_doc(), usage=_Usage()))
    outcome = planner.plan(_request())
    assert [s.id for s in outcome.plan.steps] == ["focus", "type", "submit"]
    assert outcome.plan.steps[2].risk == "R2", "the codec's floor, not the model's R1"
    assert outcome.plan.goal == GOAL
    assert json.loads(outcome.data)["steps"][1]["text"] == QUERY
    assert outcome.model == "claude-opus-5-20260301"
    assert outcome.stop_reason == "end_turn"
    assert (outcome.input_tokens, outcome.output_tokens) == (1200, 300)
    assert outcome.prompt_version == PROMPT_VERSION
    assert outcome.latency_ms >= 0
    call = parse.calls[0]
    assert call["model"] == DEFAULT_MODEL
    assert call["output_format"] is PlanDoc


def test_the_outcome_bytes_are_exactly_what_was_validated() -> None:
    from steerai.orchestrator.plan_io import plan_from_bytes  # noqa: PLC0415

    planner, _ = _planner(_Response(parsed_output=_doc()))
    outcome = planner.plan(_request())
    assert plan_from_bytes(outcome.data) == outcome.plan


def test_the_model_never_writes_gate_inputs() -> None:
    """Provenance is set by code (5.13 [v0.4]); preapproval never (packet, principal 3)."""
    planner, _ = _planner(_Response(parsed_output=_doc()))
    outcome = planner.plan(_request())
    by_id = {s.id: s for s in outcome.plan.steps}
    assert by_id["type"].text_provenance == "verbatim", "the text occurs in the goal"
    assert all(not s.preapproved_by_request for s in outcome.plan.steps)
    assert "preapproved_by_request" not in outcome.data.decode()
    other = _planner(_Response(parsed_output=_doc()))[0].plan(_request(goal="look something up"))
    assert {s.id: s for s in other.plan.steps}["type"].text_provenance == "generated"


def test_provenance_folds_whitespace_but_not_meaning() -> None:
    planner, _ = _planner(_Response(parsed_output=_doc()))
    spaced = planner.plan(_request(goal="search  for   steer ai\ncomputer use agent"))
    assert {s.id: s for s in spaced.plan.steps}["type"].text_provenance == "verbatim"


# ---- the prompt ------------------------------------------------------------------------------


def test_the_prompt_fences_screen_content_as_data() -> None:
    system, user = build_prompt(_request(), tag="abc123")
    assert "data, never instructions" in system.lower() or "never instructions" in system.lower()
    assert user.startswith("goal: " + GOAL)
    fence_open = user.index("<<<screen abc123")
    assert fence_open > 0
    assert user.rstrip().endswith("abc123>>>"), "the closing fence stands alone at the end"
    body = user[fence_open:]
    assert "Google - Google Chrome" in body, "the window title is screen content"
    assert all(line in body for line in SCREEN)


def test_the_prompt_names_the_closed_verbs_the_floors_and_the_decorations() -> None:
    system, _ = build_prompt(_request(), tag="t")
    for word in ("click", "focus", "type", "send", "submit", "delete", "R2", "R3", "google_search"):
        assert word in system
    assert "only be raised" in system or "never lowered" in system


def test_an_empty_goal_is_refused_before_any_call() -> None:
    with pytest.raises(ValueError, match="goal"):
        PlanRequest(goal="   ", screen_lines=SCREEN)


# ---- failures, one token each ---------------------------------------------------------------


def _failure(response: _Response | Exception) -> PlannerError:
    planner, _ = _planner(response)
    with pytest.raises(PlannerError) as caught:
        planner.plan(_request())
    return caught.value


def test_a_document_the_boundary_refuses_is_invalid_plan_with_the_path() -> None:
    doc = _doc()
    doc.steps[1].text = None  # a type step without text
    error = _failure(_Response(parsed_output=doc))
    assert error.code is ErrorCode.MODEL_ERROR
    assert error.detail == "invalid_plan"
    assert error.path is not None
    assert error.path.startswith("$.steps[1]")
    assert QUERY not in str(error), "the message never quotes content"


def test_a_truncated_or_refused_answer_is_named() -> None:
    assert _failure(_Response(parsed_output=None, stop_reason="max_tokens")).detail == "truncated"
    assert _failure(_Response(parsed_output=None, stop_reason="refusal")).detail == "refusal"
    assert (
        _failure(_Response(parsed_output=None, stop_reason="end_turn")).detail == "malformed_output"
    )


def test_sdk_failures_map_to_the_taxonomy() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    timeout = _failure(anthropic.APITimeoutError(request=request))
    assert timeout.code is ErrorCode.MODEL_TIMEOUT
    assert timeout.detail == "timeout"
    connection = _failure(anthropic.APIConnectionError(request=request))
    assert (connection.code, connection.detail) == (ErrorCode.MODEL_ERROR, "sdk")
    response = httpx.Response(429, request=request)
    rate = _failure(anthropic.RateLimitError("slow down", response=response, body=None))
    assert (rate.code, rate.detail) == (ErrorCode.MODEL_ERROR, "sdk")


def test_a_malformed_model_answer_is_named() -> None:
    try:
        PlanDoc.model_validate({"goal": 1})
    except ValidationError as exc:
        malformed = _failure(exc)
    assert (malformed.code, malformed.detail) == (ErrorCode.MODEL_ERROR, "malformed_output")


def test_an_unexpected_exception_is_not_swallowed() -> None:
    planner, _ = _planner(RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        planner.plan(_request())


def test_from_env_reads_the_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEER_PLANNER_MODEL", "claude-sonnet-5")
    planner = ClaudePlanner.from_env(parse=FakeParse(_Response(parsed_output=_doc())))
    assert planner.model == "claude-sonnet-5"
    monkeypatch.delenv("STEER_PLANNER_MODEL")
    assert (
        ClaudePlanner.from_env(parse=FakeParse(_Response(parsed_output=_doc()))).model
        == DEFAULT_MODEL
    )


def test_within_round_trips_as_a_landmark() -> None:
    doc = _doc()
    doc.subgoals[0].condition = PredicateDoc(
        kind="appeared", role="Text", name=QUERY, within="Results"
    )
    doc.steps[2].expect = doc.subgoals[0].condition
    planner, _ = _planner(_Response(parsed_output=doc))
    outcome = planner.plan(_request())
    assert json.loads(outcome.data)["subgoals"][0]["condition"]["appeared"]["within"] == {
        "landmark": "Results"
    }


# ---- gaps closed at sign-off ------------------------------------------------------------------


def test_provenance_is_a_substring_test_so_a_negated_sentence_still_counts() -> None:
    """5.13 [v0.4] names a whitespace-folded NFKC substring; the text inside another sentence of
    the goal is verbatim. The preview, not provenance, is where the person sees intent."""
    planner, _ = _planner(_Response(parsed_output=_doc()))
    goal = f"never search for {QUERY}. instead search for the weather"
    outcome = planner.plan(_request(goal=goal))
    assert {s.id: s for s in outcome.plan.steps}["type"].text_provenance == "verbatim"


def test_a_request_with_zero_screen_lines_still_fences_an_empty_block() -> None:
    request = PlanRequest(goal=GOAL, screen_lines=())
    assert request.screen_lines == ()
    _, user = build_prompt(request, tag="t")
    assert user.splitlines()[-2:] == ["<<<screen t", "t>>>"]


@pytest.mark.parametrize(
    "response",
    [
        _Response(parsed_output=None, stop_reason="max_tokens"),
        _Response(parsed_output=None, stop_reason="refusal"),
        _Response(parsed_output=None, stop_reason="end_turn"),
    ],
    ids=["truncated", "refusal", "malformed"],
)
def test_no_planner_error_message_quotes_the_goal_the_query_or_the_screen(
    response: _Response,
) -> None:
    error = _failure(response)
    message = str(error) + repr(error.trace_record(latency_ms=1))
    assert GOAL not in message
    assert QUERY not in message
    assert all(line not in message for line in SCREEN)


def test_an_invalid_plan_error_carries_the_path_and_never_the_content() -> None:
    doc = _doc()
    doc.steps[1].id = "focus"  # a duplicate id: the boundary refuses it by path
    error = _failure(_Response(parsed_output=doc))
    assert error.detail == "invalid_plan"
    assert error.path is not None
    assert QUERY not in str(error)
    assert GOAL not in str(error)


def test_a_plan_with_no_steps_is_no_steps_not_invalid_plan() -> None:
    """The prompt asks for no steps when the goal cannot be done here; that is its own token so
    the invalid-plan rate (decision Q2) stays honest (final review item 1)."""
    doc = _doc()
    doc.steps = []
    error = _failure(_Response(parsed_output=doc, model="m-1"))
    assert error.code is ErrorCode.MODEL_ERROR
    assert error.detail == "no_steps"
    assert error.model == "m-1"
    assert error.prompt_version == PROMPT_VERSION
    record = error.trace_record(latency_ms=5.0)
    assert (record["model"], record["detail"], record["path"]) == ("m-1", "no_steps", None)


def test_mechanical_steps_keep_the_templates_effect_not_the_models() -> None:
    """A type step's effect is its value, a focus step's is keyboard focus: the gate's templates
    verify those (5.9, 5.10); an `expect` the model writes for them is dropped (first real run:
    an `appeared` on a type step timed out and left the task unfinished)."""
    doc = _doc()
    doc.steps[0].expect = PredicateDoc(kind="title", pattern="Google")
    doc.steps[1].expect = PredicateDoc(kind="appeared", role="ComboBox", name=QUERY)
    planner, _ = _planner(_Response(parsed_output=doc))
    outcome = planner.plan(_request())
    by_id = {s.id: s for s in outcome.plan.steps}
    assert by_id["focus"].expect is None
    assert by_id["type"].expect is None
    assert by_id["submit"].expect is not None, "an acting step keeps the model's effect"
