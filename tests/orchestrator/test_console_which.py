"""The console's identity ask (PT-13/14/15/16): a fixed numbered layout with quoted, capped
screen text and tags, and a token that is an ordinal bound to the ask id."""

from __future__ import annotations

import io

import pytest

from steerai.orchestrator.console import ConsoleConfirmer, render_ask
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.identity import CandidateLine

ASK_ID = "w-0123456789ab"
MID = chr(0xB7)


def _which(*lines: CandidateLine, previous: str | None = None) -> Ask:
    return Ask(
        decision_id=ASK_ID,
        task_id="t",
        action_key="",
        verb="click",
        target_role="Hyperlink",
        risk="R1",
        expect={},
        params={},
        previous_attempt=previous,
        target_name="Amir",
        kind="which",
        candidates=lines,
    )


LINES = (
    CandidateLine(1, "Hyperlink", "Amir", f"hi bro {MID} 2h", ()),
    CandidateLine(2, "Hyperlink", "Amir Khan", f"ok {MID} 3d", ("lookalike of #3",)),
    CandidateLine(
        3, "Hyperlink", "Am" + chr(0x0456) + "r", "x" * 200, ("lookalike", "mixed script")
    ),
)


def test_the_which_ask_renders_numbered_quoted_lines_with_tags_and_the_instruction_last() -> None:
    text = render_ask(_which(*LINES))
    lines = text.splitlines()
    assert lines[0].split() == ["ASK", ASK_ID, "which", "R1", "Hyperlink"]
    assert "the plan names: Amir" in lines[1]
    assert lines[2].strip().startswith('1) Hyperlink "Amir"  preview: "hi bro')
    assert "[lookalike of #3]" in lines[3]
    assert "[lookalike, mixed script]" in lines[4]
    assert "..." in lines[4], "a 200-character preview is capped"
    assert chr(0x0456) in lines[4], "a printable letter is shown as is; the tags mark the script"
    assert lines[-1].strip().startswith("the target window holds focus")
    assert f"<n>:{ASK_ID}" in lines[-1]
    for line in text.splitlines():
        assert len(line) < 200


def test_a_previous_attempt_warns_in_the_which_ask_too() -> None:
    text = render_ask(_which(*LINES, previous="acting"))
    assert "WARNING" in text


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (f"2:{ASK_ID}\n", f"2:{ASK_ID}"),
        (f"  3:{ASK_ID}  \r\n", f"3:{ASK_ID}"),
        (f"1:{ASK_ID}", f"1:{ASK_ID}"),
        ("no\n", "no"),
        (f"no:{ASK_ID}\n", f"no:{ASK_ID}"),
        (f"0:{ASK_ID}\n", "unanswered"),
        (f"01:{ASK_ID}\n", "unanswered"),
        (f"4:{ASK_ID}\n", "unanswered"),
        (f"-1:{ASK_ID}\n", "unanswered"),
        (f"1.0:{ASK_ID}\n", "unanswered"),
        (chr(0x0661) + f":{ASK_ID}\n", "unanswered"),
        (f"1 :{ASK_ID}\n", "unanswered"),
        ("2\n", "unanswered"),
        ("yes\n", "unanswered"),
        (f"yes:{ASK_ID}\n", "unanswered"),
        ("2:w-other0000000\n", "unanswered"),
        ("\n", "unanswered"),
    ],
)
def test_only_an_ordinal_bound_to_the_ask_id_or_no_counts(answer: str, expected: str) -> None:
    confirmer = ConsoleConfirmer(io.StringIO(answer), io.StringIO(), interactive=True)
    assert confirmer.ask(_which(*LINES)) == expected


def test_a_bare_ordinal_is_unanswered_even_on_a_tty() -> None:
    confirmer = ConsoleConfirmer(io.StringIO("2\n"), io.StringIO(), interactive=True)
    assert confirmer.ask(_which(*LINES)) == "unanswered"
