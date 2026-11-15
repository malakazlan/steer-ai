"""Shared pytest configuration: platform and live-API gating, and the once-per-run Chrome warm-up
(issue #39)."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

import steerai.session.monitor  # noqa: F401 - imported before any trap: it copies `_send`
from steerai.probes.chrome_a11y import CHROME_DEFAULT, ColdStart, measure_cold_start

COLD_START = pytest.StashKey[ColdStart]()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip Windows-only tests off Windows and live tests unless explicitly selected."""
    skip_windows = pytest.mark.skip(reason="requires a Windows desktop session")
    skip_live = pytest.mark.skip(
        reason="live API test; run with -m live and TYPESAFE_API_KEY or an Anthropic credential set"
    )
    skip_probe = pytest.mark.skip(reason="launches real applications; run with -m probe")
    selected = config.getoption("-m") or ""
    for item in items:
        if "windows" in item.keywords and sys.platform != "win32":
            item.add_marker(skip_windows)
        credential = any(
            os.environ.get(name)
            for name in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        )
        live_ok = "live" in selected and credential
        if "live" in item.keywords and not live_ok:
            item.add_marker(skip_live)
        if "probe" in item.keywords and "probe" not in selected:
            item.add_marker(skip_probe)


_LIVE_MARKS = ("windows", "live", "probe")
_REAL_INPUT = (
    ("steerai.probes.win_input._send", "SendInput"),
    ("steerai.session.monitor._send", "SendInput (the monitor's self-probe)"),
    ("steerai.session.monitor._user32", "the real user32 behind the hooks and the kill hot key"),
)
"""The primitives that reach the person's real input (the monitor holds its own imports of
both); a test that is not marked live never calls them unfaked."""


@pytest.fixture(autouse=True)
def _real_input_trap(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime half of the marker guard (tests/test_live_markers.py, #359 review): a test
    with none of the live marks that reaches real input fails at once, before any event or hook
    leaves the process. A test that fakes these calls replaces the trap with its own fake."""
    if any(mark in request.node.keywords for mark in _LIVE_MARKS):
        return
    for target, what in _REAL_INPUT:

        def trap(*_args: object, _what: str = what, **_kwargs: object) -> None:
            pytest.fail(f"an unmarked test reached {_what}; mark it windows, live or probe")

        monkeypatch.setattr(target, trap)


def _needs_chrome(item: pytest.Item) -> bool:
    return (
        "warm_chrome" in getattr(item, "fixturenames", ())
        and item.get_closest_marker("skip") is None
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    """Start Chrome once before any test that needs it and time its first window (issue #39,
    schema gate item 1): here, not in a fixture, because pytest-timeout counts fixture setup
    toward a test's 60 s and on Windows ends the whole session when it fires."""
    if sys.platform != "win32" or not CHROME_DEFAULT.is_file():
        return
    if not any(_needs_chrome(item) for item in session.items):
        return
    profile = Path(tempfile.mkdtemp(prefix="steer-cold-start-"))
    measured = measure_cold_start(CHROME_DEFAULT, profile)
    session.config.stash[COLD_START] = measured
    # written now too: a run ended by pytest-timeout never reaches the summary (hunter CC-05)
    sys.stderr.write(measured.summary() + "\n")


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    """One line per run, so every CI job measures the cold start (passing runs included)."""
    measured = config.stash.get(COLD_START, None)
    if measured is not None:
        terminalreporter.write_line(measured.summary())


@pytest.fixture(scope="session")
def warm_chrome(pytestconfig: pytest.Config) -> None:
    """Chrome has shown a window once this run; when it never did, the test fails with why
    (a measured precondition, never a retry)."""
    measured = pytestconfig.stash.get(COLD_START, None)
    if measured is not None and measured.error is not None:
        pytest.fail(measured.summary())
