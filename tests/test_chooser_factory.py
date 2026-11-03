"""Chooser selection from the environment."""

import pytest

from steerai.chooser_factory import chooser_from_env
from steerai.claude_choice import ClaudeChooser
from steerai.jev import JevClient


def test_explicit_claude_wins_even_with_a_jev_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEER_DECIDER", "claude")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    chooser, label = chooser_from_env()
    assert label == "claude"
    assert isinstance(chooser, ClaudeChooser)


def test_jev_key_selects_jev_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEER_DECIDER", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    chooser, label = chooser_from_env()
    assert label == "jev"
    assert isinstance(chooser, JevClient)


def test_no_keys_falls_back_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEER_DECIDER", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    _chooser, label = chooser_from_env()
    assert label == "claude"


def test_explicit_jev_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEER_DECIDER", "jev")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(OSError, match="TYPESAFE_API_KEY not set"):
        chooser_from_env()
