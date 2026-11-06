"""The console confirmer: the pre-overlay ask surface (RN-05/06/07/08/09/10/24/41/42/43/49)."""

from __future__ import annotations

import io
from typing import Any

import pytest

from steerai.orchestrator.console import MAX_ANSWER_CHARS, ConsoleConfirmer, render_ask
from steerai.orchestrator.gate import Ask

DECISION = "d-0123456789ab"


def _ask(**overrides: Any) -> Ask:
    fields: dict[str, Any] = {
        "decision_id": DECISION,
        "task_id": "task-1",
        "action_key": "k" * 64,
        "verb": "send",
        "target_role": "Button",
        "risk": "R2",
        "expect": {"kind": "Appeared", "role": "Text", "within": {"kind": "landmark"}},
        "params": {"text": "hello there", "scope": "Conversation"},
        "target_name": "Send",
        "automation_id": "btnSend",
    }
    fields.update(overrides)
    return Ask(**fields)


def _console(
    answer: str | None, *, interactive: bool = True
) -> tuple[ConsoleConfirmer, io.StringIO]:
    stdin = io.StringIO("" if answer is None else answer)
    stderr = io.StringIO()
    return ConsoleConfirmer(stdin, stderr, interactive=interactive), stderr


# ---- what is shown -----------------------------------------------------------------------------


def test_the_first_line_is_a_parseable_ask_line_with_the_decision_id() -> None:
    confirmer, stderr = _console("no\n")
    confirmer.ask(_ask())
    first = stderr.getvalue().splitlines()[0]
    assert first.split() == ["ASK", DECISION, "send", "R2", "Button"]


def test_the_target_name_automation_id_and_text_are_shown_on_every_ask() -> None:
    confirmer, stderr = _console("no\n")
    confirmer.ask(_ask(verb="delete", risk="R3", target_name="Amir Khan", automation_id="row-7"))
    shown = stderr.getvalue()
    assert "Amir Khan" in shown
    assert "row-7" in shown
    assert "hello there" in shown
    assert f"yes:{DECISION}" in shown, "the exact token to type is shown"


def test_a_previous_attempt_is_a_distinct_warning() -> None:
    confirmer, stderr = _console("no\n")
    confirmer.ask(_ask(previous_attempt="acting"))
    assert "WARNING" in stderr.getvalue()
    assert "acting" in stderr.getvalue()
    confirmer, stderr = _console("no\n")
    confirmer.ask(_ask())
    assert "WARNING" not in stderr.getvalue()


def test_the_ask_says_the_target_window_holds_focus() -> None:
    confirmer, stderr = _console("no\n")
    confirmer.ask(_ask())
    assert "focus" in stderr.getvalue().lower()


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("hi\x1b[2Kthere", "\\x1b"),
        ("title\x1b]0;evil\x07", "\\x07"),
        ("up\rdown", "\\x0d"),
        ("zero\u200bwidth", "\\u200b"),
    ],
)
def test_control_characters_are_escaped_never_written_raw(raw: str, escaped: str) -> None:
    rendered = render_ask(_ask(target_name=raw))
    assert escaped in rendered
    for ch in rendered:
        assert ch.isprintable() or ch == "\n", repr(ch)


def test_an_unencodable_stream_gets_a_backslash_replaced_render() -> None:
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    confirmer = ConsoleConfirmer(io.StringIO("no\n"), stderr, interactive=True)
    answer = confirmer.ask(_ask(target_name="Send \U0001f600 你好"))
    stderr.flush()
    assert answer == "no"
    buffer = stderr.buffer
    assert isinstance(buffer, io.BytesIO)
    written = buffer.getvalue().decode("cp1252")
    assert "ASK" in written
    assert "\\U0001f600" in written


def test_a_render_or_write_failure_returns_unanswered_without_reading() -> None:
    class Broken(io.StringIO):
        def write(self, _text: str) -> int:
            raise OSError("stderr closed")

    stdin = io.StringIO(f"yes:{DECISION}\n")
    confirmer = ConsoleConfirmer(stdin, Broken(), interactive=True)
    assert confirmer.ask(_ask()) == "unanswered"
    assert stdin.tell() == 0, "nothing was read"


# ---- what is accepted ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [f"yes:{DECISION}\n", f"yes:{DECISION}\r\n", f"  yes:{DECISION}  \n", f"yes:{DECISION}"],
)
def test_the_bound_token_is_accepted_with_line_endings_and_padding(line: str) -> None:
    confirmer, _ = _console(line)
    assert confirmer.ask(_ask()) == f"yes:{DECISION}"


@pytest.mark.parametrize(
    "line",
    [
        f"YES:{DECISION}\n",
        f"yes :{DECISION}\n",
        "yes:d-other\n",
        "y\n",
        "\n",
        "ok\n",
        "yes please\n",
    ],
)
def test_anything_else_is_unanswered(line: str) -> None:
    confirmer, _ = _console(line)
    assert confirmer.ask(_ask()) == "unanswered"


def test_a_bare_yes_counts_only_when_a_person_is_at_the_console() -> None:
    confirmer, _ = _console("yes\n", interactive=True)
    assert confirmer.ask(_ask()) == "yes"
    confirmer, stderr = _console("yes\n", interactive=False)
    assert confirmer.ask(_ask()) == "unanswered"
    assert "not interactive" in stderr.getvalue()


@pytest.mark.parametrize("line", ["no\n", f"no:{DECISION}\n"])
def test_no_is_passed_through(line: str) -> None:
    confirmer, _ = _console(line)
    assert confirmer.ask(_ask()) == line.strip()


def test_end_of_input_is_unanswered() -> None:
    confirmer, _ = _console(None)
    assert confirmer.ask(_ask()) == "unanswered"


def test_exactly_one_line_is_consumed_per_ask_so_a_repeated_token_never_approves_twice() -> None:
    confirmer, _ = _console(f"yes:{DECISION}\nyes:{DECISION}\n")
    assert confirmer.ask(_ask()) == f"yes:{DECISION}"
    assert confirmer.ask(_ask(decision_id="d-second000000")) == "unanswered"


def test_an_overlong_line_is_unanswered() -> None:
    confirmer, _ = _console("y" * (MAX_ANSWER_CHARS + 1) + "\n")
    assert confirmer.ask(_ask()) == "unanswered"


def test_undecodable_input_is_unanswered_not_a_traceback() -> None:
    stdin = io.TextIOWrapper(io.BytesIO(b"\xff\xfe\x00yes\n"), encoding="utf-8")
    confirmer = ConsoleConfirmer(stdin, io.StringIO(), interactive=True)
    assert confirmer.ask(_ask()) == "unanswered"


def test_an_interrupt_while_waiting_is_unanswered() -> None:
    def interrupt(*_: Any) -> str:
        raise KeyboardInterrupt

    stdin = io.StringIO()
    stdin.readline = interrupt  # type: ignore[method-assign]  # a Ctrl+C stand-in on a stream
    confirmer = ConsoleConfirmer(stdin, io.StringIO(), interactive=True)
    assert confirmer.ask(_ask()) == "unanswered"


def test_the_ask_names_the_window_the_step_lands_in() -> None:
    """Step-window SW-06: the app and the reference first, the page's own title after."""
    text = render_ask(_ask(window="chrome (opened:o1): drake \u202eevil - YouTube"))
    line = next(x for x in text.splitlines() if "window:" in x)
    assert "chrome (opened:o1): drake" in line
    assert "\u202e" not in line, "format characters are shown escaped"


def test_an_ask_without_a_window_has_no_window_line() -> None:
    assert "window:" not in render_ask(_ask())
