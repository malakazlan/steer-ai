"""[V2.63] Q3: the open-IME probe is waived for stage 2, but the unprobed case fails closed. At
start v2 reads the installed input methods; when a Chinese, Japanese or Korean IME is installed
and the open-IME probe has no recorded pass on this machine that covers it, v2 refuses with
`ime_unprobed`. On an en-US-only machine it never fires (and the record is never read)."""

from __future__ import annotations

import dataclasses
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2.ime_gate import ime_refusal, read_record
from steerai.probes import ime_probe
from steerai.probes.ime_probe import RECORD_NAME
from steerai.run import EXIT_USAGE
from steerai.session.monitor import PROBE_VK
from tests.loop2.test_cli_v2 import Run

EN_US = 0x0409_0409
JA = 0x0411_0411
KO = 0x0412_0412
PASS: dict[str, Any] = {"passed": True, "probe_vk": f"{PROBE_VK:#04x}", "layouts": [f"{JA:#x}"]}


def _never() -> dict[str, Any] | None:
    raise AssertionError("an en-US-only machine never reads the record")


def test_an_en_us_only_machine_never_fires_and_never_reads_the_record() -> None:
    assert ime_refusal([EN_US], _never) is None


def test_an_installed_ime_with_no_recorded_pass_refuses() -> None:
    assert ime_refusal([EN_US, JA], lambda: None) == "ime_unprobed"


def test_a_recorded_pass_that_covers_every_installed_ime_lets_v2_start() -> None:
    assert ime_refusal([EN_US, JA], lambda: PASS) is None


@pytest.mark.parametrize(
    "record",
    [
        {**PASS, "passed": False},
        {**PASS, "probe_vk": "0x0f"},  # another probe key than the one the monitor sends
        {**PASS, "layouts": [f"{KO:#x}"]},  # the pass was for another IME
        {**PASS, "layouts": ["not-a-hkl"]},
        {"passed": "yes"},
    ],
    ids=["failed", "other_key", "other_ime", "bad_layout", "odd"],
)
def test_a_record_that_does_not_cover_this_machine_refuses(record: dict[str, Any]) -> None:
    assert ime_refusal([JA], lambda: record) == "ime_unprobed"


def test_every_installed_ime_must_be_covered() -> None:
    assert ime_refusal([JA, KO], lambda: PASS) == "ime_unprobed"


def test_an_unreadable_record_refuses() -> None:
    def broken() -> dict[str, Any] | None:
        raise OSError("unreadable")

    assert ime_refusal([JA], broken) == "ime_unprobed"


def test_the_record_lives_beside_the_price_file(tmp_path: Path) -> None:
    assert read_record(tmp_path) is None  # none written yet
    (tmp_path / RECORD_NAME).write_text(json.dumps(PASS), encoding="utf-8")
    assert read_record(tmp_path) == PASS
    (tmp_path / RECORD_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_record(tmp_path)


# ---- the probe writes the record on a pass ------------------------------------------------------


def test_a_passing_probe_records_its_layout_and_key_and_keeps_earlier_ones(tmp_path: Path) -> None:
    ime_probe.record_pass(tmp_path, KO, today="2026-11-15")
    ime_probe.record_pass(tmp_path, JA, today="2026-11-16")
    record = read_record(tmp_path)
    assert record is not None
    assert record["passed"] is True
    assert record["probe_vk"] == f"{PROBE_VK:#04x}"
    assert sorted(record["layouts"]) == sorted([f"{JA:#x}", f"{KO:#x}"])
    assert ime_refusal([JA, KO], lambda: record) is None


# ---- the command line --------------------------------------------------------------------------


def test_v2_refuses_to_start_with_its_own_reason_before_anything_is_built(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.deps = dataclasses.replace(run.deps, input_methods=lambda: [EN_US, JA],
                                   ime_record=lambda: None)  # fmt: skip
    code = run()
    assert (run.result()["detail"], run.platform.calls) == ("ime_unprobed", [])
    assert code == EXIT_USAGE  # as the price file's refusal: nothing was started


def test_an_en_us_only_machine_starts_as_before(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.deps = dataclasses.replace(run.deps, input_methods=lambda: [EN_US], ime_record=_never)
    run()
    assert run.result()["status"] != "error"


def test_the_live_deps_read_the_real_input_methods_and_record() -> None:
    from steerai.loop2 import cli  # noqa: PLC0415

    source = Path(cli.__file__).read_text(encoding="utf-8")
    body = source[source.index("def real_deps") : source.index("def main(")]
    assert "input_methods=" in body
    assert "ime_record=" in body


def _probe_main(accepted: list[int]) -> list[int]:
    recorded: list[int] = []

    def probe(_layout: int | None, _size: int, emit: Any) -> list[int]:  # Any: the row emitter
        emit({"notepad_pid": 1, "notepad_hwnd": 2, "layout": f"{JA:#x}"})
        return accepted

    with redirect_stdout(io.StringIO()):
        ime_probe.main([], probe=probe, record=recorded.append)
    return recorded


def test_a_passing_probe_run_records_the_ime_it_switched_to() -> None:
    assert _probe_main([PROBE_VK]) == [JA]


def test_a_failing_probe_run_records_nothing() -> None:
    assert _probe_main([0x0F]) == []
