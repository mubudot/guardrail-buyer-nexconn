"""Switch only the main room's transport; its service, outbox and inbound claims stay intact."""
from __future__ import annotations

from .client import MockNexconn, NexconnClient
from .upstream import ObservedNexconn, UpstreamMonitor


class ChannelTransport:
    def __init__(self, channel, upstream=None):
        self.channel = channel
        cfg = channel.cfg
        self.upstream = upstream or UpstreamMonitor(cfg.fallback, cfg.probe_every_s)
        self.live = ObservedNexconn(channel.client, self.upstream)
        self.mock = MockNexconn()
        self.upstream.set_probe(lambda: self.live.raw.issue_token(cfg.bot_user_id, "GuardRail agent"),
                                observed=isinstance(self.live.raw, NexconnClient))
        channel.client = self.live

    @property
    def mode(self):
        return "mock" if self.channel.client is self.mock else "live"

    def sync(self, *, probe=False):
        # A probe may wait on the network. Concurrent readers use the last observation.
        self.upstream.snapshot(probe=probe)
        ch = self.channel
        with ch._transport_lock:
            target = self.mock if self.upstream.offline else self.live
            if ch.client is target:
                return False
            ch.client, ch.ready, ch.last_error = target, False, None
        # The target is installed before bootstrap / retry, so their recursive sync is a no-op.
        if ch.bootstrap() and ch.client is target:
            ch.restore_pending(carried=target is self.mock)
            ch.retry_failed()
        return True

    def public(self):
        ch = self.channel
        self.sync(probe=True)
        with ch._transport_lock:
            upstream = self.upstream.snapshot()
            active = self.mode == "mock"
            return {**ch.cfg.public(), "configured_mode": ch.cfg.mode, "mode": self.mode,
                    "effective_mode": self.mode, "upstream": upstream,
                    "fallback": {"active": active, "reason": upstream["reason"] if active else None,
                                 "since": upstream["since"] if active else None}}


def configure_fallback(channel):
    """Called once by application wiring, also for channels supplied by local tests."""
    if channel.cfg.mode == "live" and channel.transport is None:
        channel.cfg.validate()
        channel.transport = ChannelTransport(channel)
