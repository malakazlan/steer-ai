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
