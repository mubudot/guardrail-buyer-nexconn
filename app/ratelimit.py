"""Startup-config errors and the in-memory rate limiter shared by the API and the /try playground.

Moved out of app/main.py (which still re-exports ConfigError, parse_rate_limit and RateLimiter)."""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class ConfigError(ValueError):
    """Invalid startup configuration (fails fast instead of crashing requests later)."""


def parse_rate_limit(value: str | None, name: str = "AGENT_RATE_LIMIT",
                     default: str = "20/60") -> tuple[int, float] | None:
    """"N/SECONDS" (e.g. "20/60") -> (N, SECONDS); "N" alone means per 60 s; "off"
    (or "none", or a bare "0") disables. N and SECONDS must be positive whole numbers;
    anything else raises ConfigError at startup (fix round 6, CODEX_REVIEW_3 #4)."""
    raw = value if value is not None else default
    v = raw.strip().lower()
    if v in ("off", "none", "0"):
        return None
    n, sep, per = v.partition("/")
    n, per = n.strip(), per.strip()
    if not re.fullmatch(r"[0-9]{1,9}", n) or (sep and not re.fullmatch(r"[0-9]{1,9}", per)) \
            or int(n) < 1 or (sep and int(per) < 1):
        raise ConfigError(f"{name}={raw!r} is invalid: use N/SECONDS with positive whole "
                          f"numbers (e.g. 20/60), or 'off' to disable")
    return int(n), float(int(per) if sep else 60)


class RateLimiter:
    """In-memory sliding-window limit per key, usually the client IP (one process only)."""

    def __init__(self, limit: int, per_seconds: float, clock=time.monotonic):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 \
                or not (isinstance(per_seconds, (int, float)) and 0 < per_seconds < float("inf")):
            raise ConfigError(f"rate limit must be a positive whole number per a positive finite "
                              f"window, got {limit!r}/{per_seconds!r}")
        self.limit, self.per, self.clock = limit, per_seconds, clock
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()
        self._swept = clock()

    def check(self, key: str) -> float | None:
        """None if allowed (and counted), else seconds until the next request is allowed."""
        now = self.clock()
        with self.lock:
            if now - self._swept > self.per:      # drop idle keys so the map cannot grow without bound
                for k in [k for k, q in self.hits.items() if not q or q[-1] <= now - self.per]:
                    del self.hits[k]
                self._swept = now
            q = self.hits[key]
            while q and q[0] <= now - self.per:
                q.popleft()
            if len(q) >= self.limit:
                return max(q[0] + self.per - now, 0.0)
            q.append(now)
            return None

    def enforce(self, key: str, what: str) -> None:
        """Raise 429 with Retry-After when `key` is over the limit."""
        wait = self.check(key)
        if wait is not None:
            raise HTTPException(429, f"rate limit: at most {self.limit} {what} per {self.per:g}s",
                                headers={"Retry-After": str(int(wait) + 1)})


def client_ip(request: Request) -> str:
    """The client address as uvicorn reports it (behind a local reverse proxy, run uvicorn with
    --proxy-headers so this is the visitor's address, not 127.0.0.1)."""
    return request.client.host if request.client else "unknown"
