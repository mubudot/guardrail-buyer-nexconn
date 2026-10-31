"""The /try playground: one click gives a visitor a private sandbox (their own Nexconn user and
approval room, their own database, simulated PayPal, the rule-based offline agent). Off unless
TRY_MODE=on. Design notes: docs/PLAYGROUND.md."""
from __future__ import annotations

from .config import PlaygroundConfig
from .manager import PlaygroundError, PlaygroundManager

__all__ = ["PlaygroundConfig", "PlaygroundError", "PlaygroundManager"]
