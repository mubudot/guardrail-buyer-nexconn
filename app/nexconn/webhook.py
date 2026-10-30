"""Nexconn webhook verification and parsing.

docs.nexconn.ai/platform-chat-api/webhook/overview: every delivery carries the headers
AppKey, Nonce, Timestamp (ms) and Signature = sha1(AppSecret + Nonce + Timestamp), and a
JSON envelope {"type", "id", "time", "data": [...]}. A `message:send` event's data items
have userId, channelId, channelType, messageType, content (string), messageId, time, os.

The signature covers only the nonce and timestamp, not the body, so on top of the check the
docs ask for we (a) refuse timestamps outside a small window and (b) refuse a nonce that was
already handled, so a captured request cannot be replayed later. A nonce is recorded only
after its delivery was handled, so a retry of a delivery we failed to handle is still
accepted; a replay of a handled one is answered 200 and ignored (no retry storm). Inbound
messages are also de-duplicated on their message id (see channel.py).

Limitation (documented in the README): because the body is not signed, an attacker who can
read a delivery in transit could reuse its headers with another body before the original
arrives. Use an https:// webhook URL; the approver allowlist and the single-use nonce
narrow what such a request could do.
"""
from __future__ import annotations

import hmac
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from .client import sign

MAX_BODY = 64 * 1024


class WebhookRejected(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class NonceCache:
    """Nonces seen within the skew window (bounded; oldest dropped first)."""

    def __init__(self, ttl_s: float, max_items: int = 10_000, clock: Callable[[], float] = time.time):
        self.ttl, self.max, self.clock = ttl_s, max_items, clock
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._seen:
            _, t = next(iter(self._seen.items()))
            if now - t <= 2 * self.ttl and len(self._seen) < self.max:
                break
            self._seen.popitem(last=False)

    def seen(self, key: str) -> bool:
        with self._lock:
            self._prune(self.clock())
            return key in self._seen

    def add(self, key: str) -> None:
        with self._lock:
            now = self.clock()
            self._prune(now)
            self._seen[key] = now


class WebhookVerifier:
    def __init__(self, app_key: str, app_secret: str, max_skew_s: int = 300,
                 clock: Callable[[], float] = time.time):
        self.app_key, self.app_secret = app_key, app_secret
        self.max_skew_ms = max_skew_s * 1000
        self.clock = clock
        self.nonces = NonceCache(max_skew_s, clock=clock)

    def verify(self, headers: dict[str, str], body: bytes) -> tuple[dict[str, Any] | None, str]:
        """Return (envelope, nonce), or (None, nonce) for a replay of a delivery already
        handled; raise WebhookRejected otherwise. `headers` keys are lower-case. Call
        `handled(nonce)` once the delivery was processed."""
        if len(body) > MAX_BODY:
            raise WebhookRejected(413, "body too large")
        nonce = headers.get("nonce") or ""
        ts = headers.get("timestamp") or ""
        sig = (headers.get("signature") or "").lower()
        key = headers.get("appkey") or headers.get("app-key") or ""
        if not (nonce and ts and sig) or len(nonce) > 64 or not ts.isdigit() or len(ts) > 16:
            raise WebhookRejected(401, "missing or malformed signature headers")
        if not hmac.compare_digest(key.encode(), self.app_key.encode()):
            raise WebhookRejected(401, "wrong AppKey")
        if not hmac.compare_digest(sig.encode(), sign(self.app_secret, nonce, ts).encode()):
            raise WebhookRejected(401, "bad signature")
        if abs(int(self.clock() * 1000) - int(ts)) > self.max_skew_ms:
            raise WebhookRejected(401, "timestamp outside the allowed window")
        if self.nonces.seen(nonce):
            return None, nonce
        return parse_envelope(body), nonce

    def handled(self, nonce: str) -> None:
        self.nonces.add(nonce)


def parse_envelope(body: bytes) -> dict[str, Any]:
    try:
        env = json.loads(body)
    except ValueError:
        raise WebhookRejected(400, "body is not JSON")
    if not isinstance(env, dict) or not isinstance(env.get("type"), str) \
            or not isinstance(env.get("data"), list):
        raise WebhookRejected(400, "not a Nexconn event envelope")
    return env


def message_events(env: dict[str, Any]) -> list[dict[str, Any]]:
    """The `message:send` items of an envelope that have the fields we rely on."""
    if env.get("type") != "message:send":
        return []
    out = []
    for item in env["data"][:50]:
        if not isinstance(item, dict):
            continue
        if all(isinstance(item.get(k), str) and item.get(k) for k in ("userId", "channelId", "messageType",
                                                                      "messageId")):
            out.append(item)
    return out
