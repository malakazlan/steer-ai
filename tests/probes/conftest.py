"""Probe unit tests never start a real suite child: a chunked run would launch a live task."""

from __future__ import annotations

import pytest

from steerai.probes import suite_chunks


def _no_child(argv: list[str]) -> suite_chunks.Child:
    raise AssertionError("a unit test started a real suite child process")


@pytest.fixture(autouse=True)
def _no_real_children(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(suite_chunks, "_start", _no_child)
