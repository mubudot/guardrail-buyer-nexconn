"""Nexconn chat approval channel for GuardRail Buyer (built for the Nexconn Build Challenge).

The agent posts each approval request into a Nexconn group chat; an approver approves or
rejects it right there; the decision runs through the same guardrailed BuyerService path as
the dashboard, and the outcome is posted back into the room. Off unless NEXCONN_MODE is set.
"""
from __future__ import annotations

import os

from .channel import ChatApprovalChannel
from .client import MockNexconn, NexconnClient, NexconnError
from .config import NexconnConfig, NexconnConfigError

__all__ = ["ChatApprovalChannel", "MockNexconn", "NexconnClient", "NexconnConfig", "NexconnConfigError",
           "NexconnError", "build_channel"]


def build_channel(svc, cfg: NexconnConfig | None = None, client=None,
                  dispatch: str = "thread") -> ChatApprovalChannel | None:
    """The channel for NEXCONN_MODE (None when off). Raises NexconnConfigError on bad settings."""
    cfg = cfg or NexconnConfig.from_env()
    if not cfg.enabled:
        return None
    if client is None:
        client = MockNexconn() if cfg.mode == "mock" else NexconnClient(cfg)
    return ChatApprovalChannel(svc, cfg, client, dispatch=dispatch)


def relay_enabled() -> bool:
    return os.environ.get("NEXCONN_RELAY") == "1"
