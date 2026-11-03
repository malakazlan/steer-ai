"""Select the System 1 element chooser from the environment.

STEER_DECIDER = "jev" (default when a TypeSafe/OpenRouter key is set) | "claude".
"""

from __future__ import annotations

import os

from steerai.claude_choice import ClaudeChooser
from steerai.decision import ElementChooser
from steerai.jev import JevClient


def chooser_from_env() -> tuple[ElementChooser, str]:
    """Return (chooser, label). Explicit STEER_DECIDER wins; else Jev with a key; else Claude."""
    choice = (os.environ.get("STEER_DECIDER") or "").strip().lower()
    if choice == "claude":
        return ClaudeChooser.from_env(), "claude"
    if choice == "jev" or os.environ.get("TYPESAFE_API_KEY"):
        return JevClient.from_env(), "jev"
    return ClaudeChooser.from_env(), "claude"
