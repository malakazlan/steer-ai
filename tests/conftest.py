"""Shared pytest configuration: platform and live-API gating."""

import os
import sys

import pytest


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
