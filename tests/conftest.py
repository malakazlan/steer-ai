"""Shared pytest configuration: platform and live-API gating."""

import os
import sys

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip Windows-only tests off Windows and live tests unless explicitly selected."""
    skip_windows = pytest.mark.skip(reason="requires a Windows desktop session")
    skip_live = pytest.mark.skip(reason="live API test; run with -m live and TYPESAFE_API_KEY set")
    live_selected = "live" in (config.getoption("-m") or "")
    for item in items:
        if "windows" in item.keywords and sys.platform != "win32":
            item.add_marker(skip_windows)
        if "live" in item.keywords and not (live_selected and os.environ.get("TYPESAFE_API_KEY")):
            item.add_marker(skip_live)
