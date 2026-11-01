"""Persistent, one-way playground transport changes, independent of the approval service."""
from __future__ import annotations

import threading

from ..ratelimit import RateLimiter
from ..nexconn.upstream import iso


class SandboxFallback:
    def __init__(self, manager, upstream):
        self.manager, self.upstream = manager, upstream
        self.sdk_limiter = RateLimiter(1, 30, clock=manager.clock)

    def snapshot(self):
        return self.upstream.snapshot(probe=True) if self.upstream else {
            "state": "live", "reason": None, "since": None}

    def ensure(self, row, *, sdk_failed=False):
        state = self.snapshot()
        if row["chat_mode"] != "live" or not self.upstream or self.upstream.fallback == "off" or \
                (state["state"] != "offline" and not sdk_failed):
            return row
        mgr, sid = self.manager, row["id"]
        with mgr._lock:
            lk = mgr._load_locks.setdefault(sid, threading.Lock())
        with lk:
            fresh = mgr.registry.get(sid)
            if fresh is None or fresh["chat_mode"] != "live" or fresh["status"] != "ACTIVE":
                return fresh or row
            if not mgr._drop(sid):
                from .manager import PlaygroundError
                raise PlaygroundError(409, "The sandbox is busy; reconnect again in a moment.")
            mgr.registry.fall_back(sid, state["since"] if state["state"] == "offline" else iso(mgr.clock()),
                                   state["reason"] or "unknown")
            return mgr.registry.get(sid)

    def info(self, row):
        upstream = self.snapshot()
        reason = row.get("fallback_reason")
        if not reason and row["chat_mode"] == "mock" and self.manager.live:
            reason = "capacity"     # old registry rows, before metadata existed
        return {"active": bool(reason), "reason": reason, "upstream": upstream["state"],
                "since": row.get("fallback_since"), "detail": row.get("fallback_detail") or "unknown"}

    def sdk_failed(self, secret):
        mgr = self.manager
        row, _ = mgr.resolve(secret)     # authenticate before probing or spending a rate-limit slot
        mgr._limit(self.sdk_limiter, row["id"], "SDK reconnect requests in a sandbox")
        if self.upstream:
            self.upstream.maybe_probe(sdk_hint=True)
        self.ensure(row, sdk_failed=True)
        return mgr.state(secret)
