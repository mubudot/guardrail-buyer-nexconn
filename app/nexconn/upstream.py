"""Process-wide upstream observations and lazy, bounded probes. Only safe reason classes escape.

The application shares one monitor between its main room and all playground rooms. Host failover
is one logical call; a reachable backup is a success. An SDK hint only requests a real API probe.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone

import httpx

from .client import NexconnClient, NexconnError


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def error_reason(error: Exception) -> str:
    status, code = getattr(error, "status", None), getattr(error, "code", None)
    # The token endpoint itself contains '/auth/'; it says nothing about the failure.
    msg = re.sub(r"(?:https?://|/)\S+", "", str(error).lower())
    if status in (401, 403) or code in (401, 403, 1002, 1003) or any(
            word in msg for word in ("auth", "app-key", "app key", "appkey", "signature", "credential",
                                    "not enabled", "expired", "expiry", "plan", "subscription")):
        return "auth"
    if isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError)) or \
            getattr(error, "reason", None) == "timeout" or any(
                word in msg for word in ("timeout", "timed out", "no api host answered", "no host answered",
                                        "transport", "connecterror")):
        return "timeout"
    if isinstance(status, int) and status >= 500:
        return "server"
    return "unknown"


def is_capacity_error(error: Exception) -> bool:
    return getattr(error, "status", None) == 429 or any(
        word in str(error).lower() for word in ("quota", "limit", "exceed", "too many"))


class UpstreamMonitor:
    def __init__(self, fallback: str = "auto", probe_every_s: float = 60, *, clock=time.time):
        if fallback not in ("auto", "off", "force"):
            raise ValueError("NEXCONN_FALLBACK: use auto, off or force")
        if probe_every_s <= 0:
            raise ValueError("NEXCONN_PROBE_EVERY_S must be positive")
        self.fallback, self.probe_every_s, self.clock = fallback, probe_every_s, clock
        self._lock = threading.Lock()
        self._state = "offline" if fallback == "force" else "live"
        self._reason = "manual" if fallback == "force" else None
        self._since = iso(clock())
        self._failures = 0
        self._last_probe = None
        self._last_hint = None
        self._next_probe = clock() + probe_every_s
        self._probing = False
        self._probe = None
        self._probe_observed = False

    def set_probe(self, probe, *, observed: bool = False) -> None:
        self._probe = probe
        self._probe_observed = observed

    @property
    def offline(self) -> bool:
        with self._lock:
            return self._state == "offline"

    def success(self, *, probe: bool = False) -> None:
        with self._lock:
            if self.fallback == "force" or (self._state == "offline" and not probe):
                return
            self._failures = 0
            if self._state != "live":
                self._state, self._reason, self._since = "live", None, iso(self.clock())

    def failure(self, error: Exception, *, registration: bool = False) -> None:
        reason = error_reason(error)
        with self._lock:
            if self.fallback != "auto" or self._state == "offline":
                return
            if reason != "auth" and registration and is_capacity_error(error):
                self._failures = 0  # a capacity refusal is an answer, not an outage
                return
            # timeout / server errors: offline after 2 in a row. An unrecognised refusal (Nexconn may
            # report an ended plan with a business code we have never seen) counts too, but needs 3
            # in a row, so one odd answer to a single call cannot switch the site over. Any success
            # resets the count.
            self._failures = self._failures + 1
            if reason == "auth" or (reason in ("timeout", "server") and self._failures >= 2) or self._failures >= 3:
                self._state, self._reason, self._since = "offline", reason, iso(self.clock())
                self._next_probe = self.clock() + self.probe_every_s

    def maybe_probe(self, *, sdk_hint: bool = False) -> bool:
        """Reserve a probe under the lock, do I/O outside it. Visitors cannot supply its result."""
        with self._lock:
            now = self.clock()
            if self.fallback != "auto" or self._probe is None or self._probing:
                return False
            if sdk_hint:
                if self._last_hint is not None and now - self._last_hint < 30:
                    return False
                self._last_hint = now
            if self._state == "offline":
                if now < self._next_probe:
                    return False
            elif not sdk_hint:
                return False
            self._probing = True
            self._last_probe = now
            self._next_probe = now + self.probe_every_s
        try:
            self._probe()
        except (NexconnError, httpx.TransportError, TimeoutError, ConnectionError) as e:
            if not self._probe_observed:
                self.failure(e, registration=True)
        else:
            self.success(probe=True)
        finally:
            with self._lock:
                self._probing = False
        return True

    def snapshot(self, *, probe: bool = False) -> dict:
        if probe:
            self.maybe_probe()
        with self._lock:
            return {"state": self._state, "reason": self._reason, "since": self._since,
                    "last_probe_at": iso(self._last_probe) if self._last_probe is not None else None,
                    "fallback": self.fallback}


class ObservedNexconn:
    """Guard normal calls while offline; allow only the monitor's raw, idempotent probe.

    The real client observes each Platform API call, including group joins. Interface fakes
    observe each public call instead, so tests never need a real Nexconn connection.
    """
    name = "nexconn-live"

    def __init__(self, client, upstream: UpstreamMonitor):
        self.raw, self.upstream = client, upstream
        if isinstance(client, NexconnClient):
            client.upstream = upstream

    def _call(self, method, *args):
        if self.upstream.offline:
            raise NexconnError("Nexconn upstream is offline")
        try:
            out = getattr(self.raw, method)(*args)
        except (NexconnError, httpx.TransportError, TimeoutError, ConnectionError) as e:
            if not isinstance(self.raw, NexconnClient):
                self.upstream.failure(e, registration=method == "issue_token")
            if not isinstance(e, NexconnError):
                raise NexconnError("Nexconn transport failed", reason="timeout") from None
            raise
        if not isinstance(self.raw, NexconnClient):
            self.upstream.success()
        return out

    def issue_token(self, user_id, name):
        return self._call("issue_token", user_id, name)

    def ensure_group(self, group_id, name, owner, members):
        return self._call("ensure_group", group_id, name, owner, members)

    def send_group_text(self, from_user, group_id, text, extra=None):
        return self._call("send_group_text", from_user, group_id, text, extra)
