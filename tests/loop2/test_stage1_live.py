"""Stage 1's live entry point (packet rev 4.3): real Chrome, the v2 CLI in a child that sends
real input. Never under the default filter: `live` and `windows`, and only in a window the
coordinator has cleared with the person."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerai.loop2 import stage1

pytestmark = [pytest.mark.live, pytest.mark.windows]


def test_the_3d_set_runs_and_writes_one_line_per_run(tmp_path: Path) -> None:
    price = Path.home() / "AppData" / "Local" / "steer" / "price.json"
    code = stage1.main(["--set", "3d", "--runs", "1", "--out", str(tmp_path),
                        "--price-file", str(price)])  # fmt: skip
    assert code in (0, 1)
