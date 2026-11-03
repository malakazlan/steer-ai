"""Select the System 1 element chooser from the environment.

STEER_DECIDER = "jev" | "claude" | unset. Unset: Jev when TYPESAFE_API_KEY exists, else Claude when
an Anthropic credential variable exists, else an error naming the variables. An explicit "claude"
trusts the SDK's own credential resolution (API key, auth token, or an `ant auth login` profile).
"""

from __future__ import annotations

import os

from steerai.claude_choice import ClaudeChooser
from steerai.decision import ElementChooser
from steerai.jev import JevClient

_ALLOWED = ("", "jev", "claude")


def chooser_from_env() -> tuple[ElementChooser, str]:
    """Return (chooser, label). Explicit STEER_DECIDER wins; else Jev with a key; else Claude."""
    choice = (os.environ.get("STEER_DECIDER") or "").strip().lower()
    if choice not in _ALLOWED:
        raise ValueError(f"STEER_DECIDER must be one of jev, claude or unset; got {choice!r}")
    if choice == "claude":
        return ClaudeChooser.from_env(), "claude"
    if choice == "jev" or os.environ.get("TYPESAFE_API_KEY"):
        return JevClient.from_env(), "jev"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return ClaudeChooser.from_env(), "claude"
    raise OSError(
        "no decision backend configured: set TYPESAFE_API_KEY (Jev) or ANTHROPIC_API_KEY (Claude), "
        "or STEER_DECIDER=claude to use an `ant auth login` profile"
    )
