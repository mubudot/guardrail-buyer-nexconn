"""Server-side Nexconn clients: the real Platform Chat API and an in-memory mock.

Both expose the same three calls the approval channel needs:

    issue_token(user_id, name) -> access token for the Web Chat SDK
    ensure_group(group_id, name, owner, members)
    send_group_text(from_user, group_id, text, extra) -> message id

Every live request is signed as documented at
docs.nexconn.ai/platform-chat-api/prepare-to-use-api/auth:
    Signature = sha1(AppSecret + Nonce + Timestamp)   (Timestamp in milliseconds)
and goes to the data center's primary host, then its backup host on a timeout or an
unexpected HTTP status (docs: base-url, "Domain failover best practices").
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from typing import Any, Callable

import httpx

from .config import NexconnConfig

GROUP_EXISTS = 25409       # "The group channel ID already exists."
ALREADY_MEMBER = 25412     # "The user is already a member of the group channel."


class NexconnError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None,
                 reason: str | None = None):
        super().__init__(message)
        self.code, self.status = code, status
        self.reason = reason


def sign(app_secret: str, nonce: str, timestamp: str) -> str:
    return hashlib.sha1(f"{app_secret}{nonce}{timestamp}".encode()).hexdigest()


def text_content(text: str, extra: dict | None = None) -> str:
    """RC:TxtMsg content as the server API wants it: a JSON object serialized to a string."""
    body: dict[str, Any] = {"content": text}
    if extra is not None:
        body["extra"] = json.dumps(extra, separators=(",", ":"))
    return json.dumps(body, ensure_ascii=False)


class NexconnClient:
    """Real Platform Chat API (server side only: it holds the App Secret)."""

    name = "nexconn-live"

    def __init__(self, cfg: NexconnConfig, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.http = http or httpx.Client(timeout=5.0)
        self.clock = clock
        self._host_idx = 0
        self._lock = threading.Lock()
        self.upstream = None

    def headers(self) -> dict[str, str]:
        nonce = secrets.token_hex(8)                     # 16 chars (docs: at most 18)
        ts = str(int(self.clock() * 1000))
        return {"App-Key": self.cfg.app_key, "Nonce": nonce, "Timestamp": ts,
                "Signature": sign(self.cfg.app_secret, nonce, ts),
                "X-Request-ID": uuid.uuid4().hex, "Content-Type": "application/json"}

    def _post(self, path: str, body: dict[str, Any],
              check: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        """One Platform API call. `check` validates the answer before it counts as a success, so a
        200 that is unusable (e.g. no token) counts toward the unrecognised-failure threshold."""
        try:
            out = self._request(path, body)
            if check is not None:
                check(out)
        except NexconnError as e:
            if self.upstream is not None:
                if e.code in (GROUP_EXISTS, ALREADY_MEMBER):
                    self.upstream.success()
                else:
                    self.upstream.failure(e, registration=path.endswith("/access-token/issue"))
            raise
        if self.upstream is not None:
            self.upstream.success()
        return out

    def _request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        hosts = self.cfg.api_hosts
        with self._lock:
            start = self._host_idx
        last: Exception | None = None
        for i in range(len(hosts)):
            idx = (start + i) % len(hosts)
            try:
                r = self.http.post(f"https://{hosts[idx]}{path}", json=body, headers=self.headers())
            except httpx.TransportError as e:            # timeout / connection: try the backup host
                last = NexconnError(f"{path}: {type(e).__name__} on {hosts[idx]}", reason="timeout")
                continue
            if r.status_code >= 500 or r.status_code in (404, 405):
                last = NexconnError(f"{path}: HTTP {r.status_code} on {hosts[idx]}", status=r.status_code)
                continue
            with self._lock:
                self._host_idx = idx                      # remember the host that answered
            try:
                data = r.json()
            except ValueError:
                raise NexconnError(f"{path}: HTTP {r.status_code}, body is not JSON", status=r.status_code)
            if not isinstance(data, dict):
                raise NexconnError(f"{path}: unexpected response shape", status=r.status_code)
            code = data.get("code")
            if r.status_code != 200 or code not in (0, 200):
                raise NexconnError(f"{path}: HTTP {r.status_code}, code {code}: "
                                   f"{str(data.get('msg') or data.get('message') or '')[:200]}",
                                   code=code if isinstance(code, int) else None, status=r.status_code)
            return data
        raise last or NexconnError(f"{path}: no API host answered", reason="timeout")

    @staticmethod
    def _token(data: dict[str, Any]) -> str:
        result = data.get("result")
        token = (result.get("accessToken") if isinstance(result, dict) else None) or data.get("token")
        if not isinstance(token, str) or not token:
            raise NexconnError("access-token/issue: response has no accessToken")
        return token

    def issue_token(self, user_id: str, name: str) -> str:
        return self._token(self._post("/v4/auth/access-token/issue", {"userId": user_id, "name": name[:64]},
                                      check=self._token))

    def ensure_group(self, group_id: str, name: str, owner: str, members: list[str]) -> None:
        invite = [m for m in dict.fromkeys(members) if m != owner]
        if len(invite) > 30:                              # the create call invites at most 30 users
            raise NexconnError("at most 30 members besides the owner")
        try:
            self._post("/v4/group-channel/create", {"channelId": group_id, "name": name, "owner": owner,
                                                    "userIds": invite})
            return
        except NexconnError as e:
            if e.code != GROUP_EXISTS:
                raise
        # group already there (earlier run, or other approvers then): add each member, one by one so
        # an "already a member" answer for one user cannot hide a missing one
        for m in [owner, *invite]:
            try:
                self._post("/v4/group-channel/join", {"channelId": group_id, "userIds": [m]})
            except NexconnError as e:
                if e.code != ALREADY_MEMBER:
                    raise

    @staticmethod
    def _message_id(data: dict[str, Any], group_id: str) -> str:
        result = data.get("result")
        ids = result.get("messageIds") if isinstance(result, dict) else None
        ids = [m for m in ids if isinstance(m, dict) and m.get("messageId")] if isinstance(ids, list) else []
        for m in ids:
            if m.get("channelId") == group_id:
                return str(m["messageId"])
        if ids:
            return str(ids[0]["messageId"])
        raise NexconnError("group-channel/message/send: response has no messageId")

    def send_group_text(self, from_user: str, group_id: str, text: str, extra: dict | None = None) -> str:
        # the message id is validated inside the monitored call, so an unusable 200 counts toward the
        # unrecognised-failure threshold instead of being recorded as a success
        data = self._post("/v4/group-channel/message/send", {
            "fromUserId": from_user, "toChannelIds": [group_id], "messageType": "RC:TxtMsg",
            "content": text_content(text, extra), "shouldPersist": 1, "isEchoToSender": 0},
            check=lambda d: self._message_id(d, group_id))
        return self._message_id(data, group_id)


class MockNexconn:
    """In-memory stand-in with the same calls, plus the room's message log so the offline
    /chat page (and the tests) can read it. Not a Nexconn emulator: it only keeps what the
    approval flow needs (one group, text messages with `extra`)."""

    name = "nexconn-mock"

    TOKENS_PER_USER = 4                  # older session tokens of a user are revoked

    def __init__(self, clock: Callable[[], float] = time.time, max_group_messages: int | None = None,
                 max_total_messages: int | None = None):
        self.clock = clock
        self.lock = threading.Lock()
        self.groups: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.tokens: dict[str, str] = {}
        self._user_tokens: dict[str, list[str]] = {}
        self._group_counts: dict[str, int] = {}
        # Memory caps (None = unbounded, the single-room /chat demo). The /try playground sets both,
        # so a visitor cannot grow the shared process without bound.
        self.max_group_messages, self.max_total_messages = max_group_messages, max_total_messages
        self.fail_next_send = 0          # tests: make the next N sends fail

    def issue_token(self, user_id: str, name: str) -> str:
        with self.lock:
            tok = f"mock-{secrets.token_urlsafe(16)}"
            self.tokens[tok] = user_id
            mine = self._user_tokens.setdefault(user_id, [])
            mine.append(tok)
            while len(mine) > self.TOKENS_PER_USER:
                self.tokens.pop(mine.pop(0), None)
            return tok

    def revoke_user(self, user_id: str) -> None:
        """Invalidate every session token of a user (the /try playground, when a sandbox expires)."""
        with self.lock:
            for tok in self._user_tokens.pop(user_id, []):
                self.tokens.pop(tok, None)

    def user_for_token(self, token: str) -> str | None:
        with self.lock:
            return self.tokens.get(token)

    def ensure_group(self, group_id: str, name: str, owner: str, members: list[str]) -> None:
        with self.lock:
            g = self.groups.setdefault(group_id, {"name": name, "owner": owner, "members": set()})
            g["members"].update(members)
            g["members"].add(owner)

    def send_group_text(self, from_user: str, group_id: str, text: str, extra: dict | None = None) -> str:
        with self.lock:
            if self.fail_next_send > 0:
                self.fail_next_send -= 1
                raise NexconnError("mock: send failed (test)")
            if group_id not in self.groups:
                raise NexconnError("mock: group does not exist", code=25410)
            n = self._group_counts.get(group_id, 0)
            if (self.max_group_messages is not None and n >= self.max_group_messages) or \
                    (self.max_total_messages is not None and len(self.messages) >= self.max_total_messages):
                raise NexconnError("mock: room message limit reached", status=429)
            self._group_counts[group_id] = n + 1
            mid = f"MOCK-{uuid.uuid4().hex[:12].upper()}"
            self.messages.append({
                "messageId": mid, "userId": from_user, "channelId": group_id, "channelType": 3,
                "messageType": "RC:TxtMsg", "content": text_content(text, extra),
                "time": int(self.clock() * 1000), "os": "Server"})
            return mid

    def group_count(self, group_id: str) -> int:
        with self.lock:
            return self._group_counts.get(group_id, 0)

    def drop_group(self, group_id: str) -> None:
        """Forget a group and its messages (the /try playground, when a sandbox expires)."""
        with self.lock:
            self.groups.pop(group_id, None)
            self._group_counts.pop(group_id, None)
            self.messages = [m for m in self.messages if m["channelId"] != group_id]

    def history(self, group_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(m) for m in self.messages if m["channelId"] == group_id][after:]
