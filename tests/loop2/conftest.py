"""Shared fixtures for the v2 loop tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from steerai.loop2 import form


@pytest.fixture(autouse=True)
def _cold_form_memo() -> Iterator[None]:
    """#319 review S3-19: every test starts with the risky-reading memo empty, so no test passes
    on a reading a previous one left behind."""
    form.clear_memo()
    yield
    form.clear_memo()
