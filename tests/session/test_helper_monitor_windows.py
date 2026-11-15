"""[V2.60] the helper monitor's live session reads (#383 principal note 3). Read-only: WTS and the
input desktop's name, no hooks, no input.

The first poll flags any start state that is not (unlocked, `Default`) as a transition
(`_OURS`, HM-01), so a session that reports `WTS_SESSIONSTATE_UNKNOWN` would stop every task.
This proves the reads on the CI runner before the monitor is wired in."""

from __future__ import annotations

import pytest

from steerai.session.helper_monitor import WindowsSessionReads

pytestmark = pytest.mark.windows


def test_this_session_reads_as_unlocked() -> None:
    assert WindowsSessionReads().lock_state() == 1  # WTS_SESSIONSTATE_UNLOCK


def test_the_input_desktop_reads_as_default() -> None:
    assert WindowsSessionReads().input_desktop() == "Default"
