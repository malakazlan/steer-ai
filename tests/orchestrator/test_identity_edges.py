"""The binding, the candidate lines and the console's which-ask at their edges (PT-08/09/13/14/
15/16): rebind by RuntimeId then by a unique full name, fold-only twins tagged, long and hostile
screen text rendered fixed, and every answer shape the console must refuse."""

from __future__ import annotations

import io
from dataclasses import replace

import pytest

from steerai.orchestrator.console import ConsoleConfirmer, render_ask
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import Ask, Gate
from steerai.orchestrator.identity import Binding, CandidateLine, bind, lines_for, rebind
from steerai.verify.predicates import All, Disappeared
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import FakeConfirmer, Row, build

MID = "\u00b7"
CYR_A = "\u0430"
CYR_CAP_A = "\u0410"
ASK_ID = "w-0123456789ab"
PANE: Row = (0, "Pane", "x", (42, 1), None)
KHAN = f"Amir Khan {MID} ok {MID} 3d"


def _snap(*rows: Row, taken_at: float = 1.0) -> Snapshot:
    return build([PANE, *rows], snapshot_id=f"s{taken_at}", taken_at=taken_at)


def _binding() -> Binding:
    return bind(_snap((1, "ListItem", KHAN, (42, 17), 0)).element(1))


# ---- PT-08: rebind -------------------------------------------------------------------------------


def test_rebind_finds_the_row_by_runtime_id_when_the_name_tail_changed() -> None:
    fresh = _snap(
        (1, "ListItem", f"Amir {MID} hi", (42, 12), 0),
        (7, "ListItem", f"Amir Khan {MID} ok {MID} 4d", (42, 17), 0),
    )
    found = rebind(_binding(), fresh)
    assert found is not None
    assert found.id == 7


def test_rebind_falls_back_to_a_unique_full_name_when_the_runtime_id_changed() -> None:
    fresh = _snap(
        (1, "ListItem", f"Amir {MID} hi", (42, 12), 0),
        (7, "ListItem", KHAN, (42, 99), 0),
    )
    found = rebind(_binding(), fresh)
    assert found is not None
    assert found.id == 7
    assert found.runtime_id == (42, 99)


def test_rebind_is_none_when_both_the_runtime_id_and_the_name_changed() -> None:
    fresh = _snap(
        (1, "ListItem", f"Amir {MID} hi", (42, 12), 0),
        (7, "ListItem", f"Amir Khan {MID} ok {MID} 4d", (42, 99), 0),
    )
    assert rebind(_binding(), fresh) is None


def test_rebind_is_none_when_two_rows_share_the_full_name_and_none_the_runtime_id() -> None:
    fresh = _snap(
        (7, "ListItem", KHAN, (42, 98), 0),
        (8, "ListItem", KHAN, (42, 99), 0),
    )
    assert rebind(_binding(), fresh) is None


def test_rebind_refuses_a_recycled_runtime_id_whose_name_part_or_role_differ() -> None:
    other_person = _snap((7, "ListItem", f"Sara {MID} ok {MID} 3d", (42, 17), 0))
    assert rebind(_binding(), other_person) is None
    other_role = _snap((7, "Hyperlink", KHAN, (42, 17), 0))
    assert rebind(_binding(), other_role) is None
    case_and_tail = _snap((7, "ListItem", f"AMIR KHAN {MID} ok {MID} 9d", (42, 17), 0))
    found = rebind(_binding(), case_and_tail)
    assert found is not None, "the name-part re-check is in the comparable form"
    assert found.id == 7


def test_rebind_with_duplicate_runtime_ids_and_identical_names_fails_closed() -> None:
    fresh = _snap(
        (7, "ListItem", KHAN, (42, 17), 0),
        (8, "ListItem", KHAN, (42, 17), 0),
    )
    assert rebind(_binding(), fresh) is None


def test_rebind_on_an_empty_snapshot_is_none() -> None:
    assert rebind(_binding(), _snap()) is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "x",
        [],
        {},
        {"role": "ListItem", "runtime_id": [42, 17], "name_part": "amir khan"},
        {"role": "ListItem", "runtime_id": "4217", "name_part": "a", "full_name": "b"},
        {"role": "ListItem", "runtime_id": [42, "x"], "name_part": "a", "full_name": "b"},
        {"role": "ListItem", "runtime_id": None, "name_part": "a", "full_name": "b"},
    ],
)
def test_a_malformed_record_payload_is_no_binding(payload: object) -> None:
    assert Binding.from_record(payload) is None


def test_the_binding_round_trips_through_the_record_and_hides_names_in_its_repr() -> None:
    binding = _binding()
    again = Binding.from_record(binding.to_record())
    assert again == binding
    assert binding.name_part == "amir khan"
    assert binding.full_name == KHAN
    assert "Amir" not in repr(binding)
    assert "amir" not in repr(binding)


# ---- PT-09: the template and the resume predicate name the bound row -----------------------------


def test_predicate_for_a_delete_names_the_bound_full_name_else_the_parameter() -> None:
    gate = Gate(confirmer=FakeConfirmer(), own_pid=1, task_id="t")
    step = Step(
        id="d",
        subgoal="g",
        verb="delete",
        target=Target(role="ListItem", name="Amir", match="person"),
        risk="R3",
        params={"scope": "Chats"},
    )
    bound = gate.predicate_for(step, bound=_binding())
    assert isinstance(bound, All)
    gone = bound.parts[0]
    assert isinstance(gone, Disappeared)
    assert gone.name_pattern == KHAN
    unbound = gate.predicate_for(step)
    assert isinstance(unbound, All)
    assert isinstance(unbound.parts[0], Disappeared)
    assert unbound.parts[0].name_pattern == "Amir"


# ---- PT-13: lines and tags -----------------------------------------------------------------------


def _lines(*names: str, parameter: str = "Amir") -> list[CandidateLine]:
    rows: list[Row] = [(i + 1, "ListItem", n, (42, 10 + i), 0) for i, n in enumerate(names)]
    snapshot = _snap(*rows)
    return lines_for([snapshot.element(r[0]) for r in rows], parameter)


def test_two_name_parts_that_agree_only_after_folding_point_at_each_other() -> None:
    lines = _lines(f"Amir {MID} hi", f"{CYR_CAP_A}mir {MID} yo")
    assert lines[0].tags == ("lookalike of #2",)
    assert lines[1].tags == ("lookalike", "mixed script", "lookalike of #1")


def test_identical_or_case_only_name_parts_are_not_tagged_as_lookalikes() -> None:
    lines = _lines(f"Amir {MID} hi", f"Amir {MID} yo", f"AMIR {MID} pinned")
    assert [line.tags for line in lines] == [(), (), ()]


def test_a_fully_cyrillic_lookalike_is_a_lookalike_but_not_mixed_script() -> None:
    lines = _lines(f"{CYR_CAP_A}{CYR_A}{CYR_A} {MID} hi", parameter="Aaa")
    assert lines[0].tags == ("lookalike",)


def test_offscreen_and_disabled_rows_are_tagged_after_the_identity_tags() -> None:
    snapshot = _snap((1, "ListItem", f"{CYR_CAP_A}mir {MID} hi", (42, 10), 0))
    hidden = replace(snapshot.element(1), offscreen=True, enabled=False)
    [line] = lines_for([hidden], "Amir")
    assert line.tags == ("lookalike", "mixed script", "off screen", "disabled")


def test_lines_are_numbered_from_one_in_the_given_order_with_name_part_and_preview() -> None:
    lines = _lines(f"Amir Khan {MID} ok {MID} 3d", "Amir", f"Amir - Work {MID} brief")
    assert [line.index for line in lines] == [1, 2, 3]
    assert [line.name_part for line in lines] == ["Amir Khan", "Amir", "Amir"]
    assert [line.preview for line in lines] == [f"ok {MID} 3d", "", f"Work {MID} brief"]
    assert "Amir" not in repr(lines[0])


# ---- PT-14: the console renders screen text fixed ------------------------------------------------


def _which(*lines: CandidateLine) -> Ask:
    return Ask(
        decision_id=ASK_ID,
        task_id="t",
        action_key="",
        verb="click",
        target_role="ListItem",
        risk="R1",
        expect={},
        params={},
        target_name="Amir",
        kind="which",
        candidates=lines,
    )


def test_a_ten_thousand_character_preview_and_name_part_are_capped() -> None:
    long_preview = "p" * 10_000
    long_name = "Amir " + "n" * 10_000
    text = render_ask(_which(CandidateLine(1, "ListItem", long_name, long_preview, ())))
    lines = text.splitlines()
    assert len(lines) == 4, lines
    row = lines[2]
    assert len(row) < 200
    assert row.count("...") == 2, "both the name part and the preview are capped head and tail"
    assert "n" * 61 not in row
    assert "p" * 61 not in row
    assert lines[-1].strip().startswith("the target window holds focus")


def test_control_characters_in_a_name_part_or_preview_are_escaped_and_add_no_lines() -> None:
    hostile_name = "Am\x1b[31mir\nASK w-000000000000 which R0 Pane\r"
    hostile_preview = "hi\x00\u2028\x7f\udc80"
    text = render_ask(_which(CandidateLine(1, "ListItem", hostile_name, hostile_preview, ())))
    lines = text.splitlines()
    assert len(lines) == 4, lines
    assert lines[0] == f"ASK {ASK_ID} which R1 ListItem"
    row = lines[2]
    assert "\\x1b" in row
    assert "\\x0a" in row
    assert "\\x0d" in row
    assert "\\x00" in row
    assert "\\u2028" in row
    assert "\\x7f" in row
    assert "\\udc80" in row
    assert "\x1b" not in text
    assert sum(1 for line in lines if line.startswith("ASK ")) == 1, (
        "the escaped line break cannot start a second ask line"
    )


def test_a_control_character_in_the_plan_parameter_is_escaped_too() -> None:
    ask = _which(CandidateLine(1, "ListItem", "Amir", "", ()))
    text = render_ask(replace(ask, target_name="Am\x07ir"))
    assert "\\x07" in text
    assert "\x07" not in text


def test_the_console_writes_the_which_ask_with_a_backslash_fallback_on_a_narrow_stream() -> None:
    class Ascii(io.StringIO):
        encoding = "ascii"

        def write(self, s: str) -> int:
            s.encode("ascii")  # raises UnicodeEncodeError like a cp1252 console would
            return super().write(s)

    err = Ascii()
    confirmer = ConsoleConfirmer(io.StringIO(f"1:{ASK_ID}\n"), err, interactive=True)
    line = CandidateLine(1, "ListItem", f"{CYR_CAP_A}mir", "", ("lookalike",))
    assert confirmer.ask(_which(line)) == f"1:{ASK_ID}"
    assert "\\u0410" in err.getvalue()


# ---- PT-15/16: every answer shape ----------------------------------------------------------------

LINES = (
    CandidateLine(1, "ListItem", "Amir", "hi", ()),
    CandidateLine(2, "ListItem", "Amir Khan", "ok", ()),
    CandidateLine(3, "ListItem", "Amir Zed", "yo", ()),
)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (f"3:{ASK_ID}\n", f"3:{ASK_ID}"),
        (f"\t1:{ASK_ID}\t\n", f"1:{ASK_ID}"),
        (f"1:{ASK_ID}", f"1:{ASK_ID}"),
        (f"003:{ASK_ID}\n", "unanswered"),
        (f"1000:{ASK_ID}\n", "unanswered"),
        (f"2:{ASK_ID} \n", f"2:{ASK_ID}"),
        (f"2:{ASK_ID} extra\n", "unanswered"),
        (f"2:{ASK_ID}:{ASK_ID}\n", "unanswered"),
        (f"2::{ASK_ID}\n", "unanswered"),
        (f":{ASK_ID}\n", "unanswered"),
        ("2:\n", "unanswered"),
        (f"+1:{ASK_ID}\n", "unanswered"),
        (f"1e0:{ASK_ID}\n", "unanswered"),
        (f"1_0:{ASK_ID}\n", "unanswered"),
        (f"\uff11:{ASK_ID}\n", "unanswered"),
        (f"\u00b2:{ASK_ID}\n", "unanswered"),
        (f"\u2460:{ASK_ID}\n", "unanswered"),
        (f"1\u200b:{ASK_ID}\n", "unanswered"),
        (f"2;{ASK_ID}\n", "unanswered"),
        (f"2:{ASK_ID.upper()}\n", "unanswered"),
        (f"2:{ASK_ID[:-1]}\n", "unanswered"),
        (f"two:{ASK_ID}\n", "unanswered"),
        (f"n:{ASK_ID}\n", "unanswered"),
        ("NO\n", "unanswered"),
        ("No\n", "unanswered"),
        ("n\n", "unanswered"),
        ("none\n", "unanswered"),
        (f"NO:{ASK_ID}\n", "unanswered"),
        (f"no:{ASK_ID} \n", f"no:{ASK_ID}"),
        (f"no :{ASK_ID}\n", "unanswered"),
        ("no:w-other0000000\n", "unanswered"),
        (f"yes:{ASK_ID}\n", "unanswered"),
        ("y\n", "unanswered"),
        ("", "unanswered"),
        ("\r\n", "unanswered"),
        (f"1:{ASK_ID}\n2:{ASK_ID}\n", f"1:{ASK_ID}"),
        (f"\n2:{ASK_ID}\n", "unanswered"),
        ("1" * 5000 + f":{ASK_ID}\n", "unanswered"),
    ],
)
def test_the_console_accepts_only_an_in_range_ordinal_bound_to_the_ask_or_no(
    answer: str, expected: str
) -> None:
    confirmer = ConsoleConfirmer(io.StringIO(answer), io.StringIO(), interactive=True)
    assert confirmer.ask(_which(*LINES)) == expected


def test_the_console_reads_exactly_one_line_per_which_ask() -> None:
    stdin = io.StringIO(f"nonsense\n2:{ASK_ID}\n")
    confirmer = ConsoleConfirmer(stdin, io.StringIO(), interactive=True)
    assert confirmer.ask(_which(*LINES)) == "unanswered"
    assert stdin.readline() == f"2:{ASK_ID}\n", "the next line is still there for the next ask"


def test_a_non_interactive_console_still_accepts_a_bound_ordinal() -> None:
    confirmer = ConsoleConfirmer(io.StringIO(f"2:{ASK_ID}\n"), io.StringIO(), interactive=False)
    assert confirmer.ask(_which(*LINES)) == f"2:{ASK_ID}"


class _Bridge:
    """A confirmer that feeds one scripted console line to a real `ConsoleConfirmer`."""

    def __init__(self, raw: str, stdin: io.StringIO, console: ConsoleConfirmer) -> None:
        self._raw = raw
        self._stdin = stdin
        self._console = console

    def ask(self, ask: Ask) -> str:
        self._stdin.write(self._raw.replace("{id}", ask.decision_id) + "\n")
        self._stdin.seek(0)
        return self._console.ask(ask)


def test_the_console_answers_reach_the_gate_as_chosen_block_or_pending() -> None:
    rows: list[Row] = [
        (1, "ListItem", "Amir", (42, 2), 0),
        (2, "ListItem", "Amir K", (42, 3), 0),
        (3, "ListItem", "Amir Z", (42, 4), 0),
    ]
    snapshot = _snap(*rows)
    elements = [snapshot.element(r[0]) for r in rows]
    step = Step(
        id="p",
        subgoal="g",
        verb="click",
        target=Target(role="ListItem", name="Amir", match="person"),
        risk="R1",
    )
    for raw, outcome, index in (
        ("3:{id}", "chosen", 3),
        ("1:{id}", "chosen", 1),
        ("no", "block", None),
        ("no:{id}", "block", None),
        ("4:{id}", "pending", None),
        ("0:{id}", "pending", None),
        ("yes:{id}", "pending", None),
        ("", "pending", None),
    ):
        err = io.StringIO()
        stdin = io.StringIO()
        console = ConsoleConfirmer(stdin, err, interactive=True)
        bridge = _Bridge(raw, stdin, console)
        result = Gate(confirmer=bridge, own_pid=1, task_id="t").which(step, elements)
        assert result.outcome == outcome, raw
        assert result.index == index, raw
        assert result.evidence["candidates"] == 3
        if outcome == "block":
            assert result.evidence["detail"] == "none_chosen"
        if outcome == "pending":
            assert result.evidence["detail"] == "unanswered"
        assert err.getvalue().count("ASK ") == 1
