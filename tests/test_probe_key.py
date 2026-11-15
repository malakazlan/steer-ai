"""[V2.57] LW-01: the probe that picks the keyboard hook's probe key. It sends real input (into
its own Notepad), so it runs only when asked for: `-m probe` on Windows."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from steerai.probes import probe_key


@pytest.mark.probe
@pytest.mark.windows
def test_the_probe_reports_each_unassigned_key_and_the_first_that_changes_nothing() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        code = probe_key.main(["--batch", "20"])
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code in (0, 1, 2)
    assert all(row["probe"] == "probe_key" for row in rows)
    if code == 0:
        assert rows[-1]["first"] in rows[-1]["accepted"]


# ---- the load modes, pure: nothing here sends input ----------------------------------------------


def test_the_json_load_parses_a_payload_of_about_200_kb() -> None:
    payload = probe_key.json_payload()
    assert 180_000 <= len(payload) <= 220_000
    assert isinstance(json.loads(payload), dict)


@pytest.mark.parametrize("load", ["none", "spin", "json"])
def test_each_load_runs_until_stopped(load: str) -> None:
    with probe_key.load_running(load) as worked:
        import time  # noqa: PLC0415 - this test only

        time.sleep(0.05)
    assert (worked() > 0) is (load != "none")


def test_an_unknown_load_is_refused() -> None:
    with pytest.raises(ValueError, match="load"), probe_key.load_running("disk"):
        pass


def test_the_fast_switch_loads_set_the_interval_only_inside_and_restore_it() -> None:
    """[V2.59] (iv): the spinner and the parse again with sys.setswitchinterval(0.001)."""
    import sys  # noqa: PLC0415 - this test only

    before = sys.getswitchinterval()
    assert {"spin_fast", "json_fast"} <= set(probe_key.LOADS)
    with probe_key.load_running("spin_fast") as worked:
        assert sys.getswitchinterval() == pytest.approx(0.001)
        import time  # noqa: PLC0415 - this test only

        time.sleep(0.02)
    assert worked() > 0
    assert sys.getswitchinterval() == before


def test_echoes_over_the_deadline_are_counted() -> None:
    assert probe_key.over_deadline([1.0, 300.0, 1050.0, 1051.0], deadline_ms=1050.0) == 1
