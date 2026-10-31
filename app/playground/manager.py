"""PlaygroundManager: creates, resumes, joins, resets and expires sandboxes, keeps an LRU of open
ones (the rest reload lazily from disk), and routes room messages of playground groups.

Everything needed to resume a sandbox is on disk (TRY_DIR/registry.db + TRY_DIR/<id>.db), so a
restart loses only the in-memory simulated PayPal orders (marked MOCK_RESET, as in the main app's
mock mode) and, for rooms on the offline stand-in, the stand-in's message history.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import string
import threading
import time
from collections import Counter, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..catalog import Catalog
from ..guardrails import Policy
from ..nexconn.client import MockNexconn, NexconnError
from ..nexconn.config import NexconnConfig
from ..nexconn.routes import _clean_extra, _item
from ..paypal_client import MockPayPal, PayPalError
from ..ratelimit import RateLimiter
from .config import PlaygroundConfig
from .registry import MembersFull, Registry
from .sandbox import Sandbox

log = logging.getLogger("guardrail.playground")

GROUP_PREFIX = "grbtry"
_ALNUM = string.ascii_lowercase + string.digits
_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,24}")
SWEEP_EVERY_S = 600
RETRY_EVERY_S = 30
DB_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db.mock.lock")
MOCK_ROOM_MESSAGES = 1000        # offline stand-in: messages kept per sandbox room (reset clears nothing here;
MOCK_TOTAL_MESSAGES = 20000      # the room just refuses new sends) and across all rooms (~20 MB at worst)
MOCK_SEND_LIMIT = (30, 60.0)     # offline stand-in: room sends per sandbox
CHECKOUT_LIMIT_IP = (120, 600.0) # simulated checkout requests per client IP


class PlaygroundError(Exception):
    """An expected refusal, mapped to an HTTP status by the routes."""

    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers


def clean_name(raw: str | None) -> str:
    """Display name: stripped, at most 24 of letters, digits, space, '.', '-', '_'; else Guest <hex>."""
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if name and _NAME.fullmatch(name):
        return name
    return f"Guest {secrets.token_hex(2)}"


def new_user_id() -> str:
    """Nexconn user ids are letters and digits only (max 64): 't' + 15 random lowercase alnum."""
    return "t" + "".join(secrets.choice(_ALNUM) for _ in range(15))


def is_capacity_error(e: NexconnError) -> bool:
    """A Nexconn refusal that means "no more users / quota" (fall back to the offline stand-in)
    rather than an outage (answer 502 and leave nothing half-created)."""
    msg = str(e).lower()
    return e.status == 429 or any(w in msg for w in ("quota", "limit", "exceed", "too many"))


def delete_sandbox_files(directory: Path, sandbox_id: str) -> None:
    for suffix in DB_SUFFIXES:
        try:
            (directory / f"{sandbox_id}{suffix}").unlink()
        except FileNotFoundError:
            pass


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class PlaygroundManager:
    def __init__(self, pcfg: PlaygroundConfig, base_cfg: NexconnConfig, *, live_client=None,
                 mock_client: MockNexconn | None = None, policy: Policy, catalog: Catalog,
                 clock=time.time):
        self.cfg, self.base_cfg = pcfg, base_cfg
        self.policy, self.catalog, self.clock = policy, catalog, clock
        pcfg.dir.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(pcfg.dir / "registry.db", clock)
        self.live = base_cfg.mode == "live" and live_client is not None
        self.live_client = live_client
        self.mock = mock_client or MockNexconn()
        if self.mock.max_group_messages is None:
            self.mock.max_group_messages = MOCK_ROOM_MESSAGES
        if self.mock.max_total_messages is None:
            self.mock.max_total_messages = MOCK_TOTAL_MESSAGES
        self._lock = threading.RLock()
        self._cache: OrderedDict[str, Sandbox] = OrderedDict()
        self._inuse: Counter = Counter()
        self._load_locks: dict[str, threading.Lock] = {}
        self._paypals: dict[str, MockPayPal] = {}
        self._bot_registered = False
        self._last_sweep = 0.0
        lim = lambda v: RateLimiter(*v) if v else None  # noqa: E731
        self.create_limiter, self.join_limiter = lim(pcfg.create_limit), lim(pcfg.join_limit)
        self.agent_limiter, self.agent_ip_limiter = lim(pcfg.agent_limit), lim(pcfg.agent_limit_ip)
        self.mock_send_limiter, self.checkout_limiter = lim(MOCK_SEND_LIMIT), lim(CHECKOUT_LIMIT_IP)
        self.closed = False
        self.sweep(force=True)

    # ---- helpers ----------------------------------------------------------------
    @staticmethod
    def _limit(limiter: RateLimiter | None, key: str, what: str) -> None:
        if limiter is not None:
            wait = limiter.check(key)
            if wait is not None:
                raise PlaygroundError(429, f"You're going fast: at most {limiter.limit} {what} per "
                                           f"{limiter.per:g}s. Try again in a moment.",
                                      {"Retry-After": str(int(wait) + 1)})

    def _alive(self, row: dict[str, Any] | None) -> bool:
        """ACTIVE and not idle past the TTL (the sweep runs at most every 10 minutes, so every access
        checks the idle time itself instead of trusting the status alone)."""
        return row is not None and row["status"] == "ACTIVE" and self.clock() - row["last_seen"] <= self.cfg.ttl_s

    def _gone(self, row: dict[str, Any] | None) -> PlaygroundError:
        if row is not None and row["status"] == "ACTIVE":     # idle past the TTL: expire it now
            try:
                self.expire(row["id"])
            except Exception:   # pragma: no cover - the next sweep retries
                log.exception("playground: expiring %s failed", row["id"])
        return PlaygroundError(404, "Your sandbox expired. Start a new one.")

    def invite_url(self, sandbox_id: str) -> str:
        return f"{self.cfg.public_base_url}/try/join/{self.registry.invite_code(sandbox_id)}"

    def _client_for(self, chat_mode: str):
        return self.live_client if chat_mode == "live" else self.mock

    def _paypal(self, sid: str) -> MockPayPal:
        with self._lock:
            pp = self._paypals.get(sid)
            if pp is None:
                pp = self._paypals[sid] = MockPayPal(
                    require_payer_approval=True, checkout_base=f"{self.cfg.public_base_url}/try/s/{sid}")
            return pp

    def live_seats_left(self) -> int:
        if not self.live:
            return 0
        return max(self.cfg.max_live_users - self.registry.counter("live_users_registered"), 0)

    def _register(self, user_id: str, name: str) -> None:
        """Register one Nexconn user (live): the bot once per process, then the visitor."""
        if not self._bot_registered:
            self.live_client.issue_token(self.base_cfg.bot_user_id, "GuardRail agent")
            self._bot_registered = True
        self.live_client.issue_token(user_id, name)

    # ---- loading (LRU of open sandboxes) -------------------------------------------
    @contextmanager
    def use(self, sid: str) -> Iterator[Sandbox]:
        if self.closed:
            raise PlaygroundError(503, "The playground is shutting down; try again in a few seconds.")
        row = self.registry.get(sid)
        if not self._alive(row):          # checked before any per-sandbox state (lock, cache) is created
            raise self._gone(row)
        with self._lock:
            sb = self._cache.get(sid)
            if sb is not None:
                self._cache.move_to_end(sid)
                self._inuse[sid] += 1
            else:
                lk = self._load_locks.setdefault(sid, threading.Lock())
        if sb is None:
            with lk:
                with self._lock:
                    sb = self._cache.get(sid)
                    if sb is not None:
                        self._inuse[sid] += 1
                if sb is None:
                    row = self.registry.get(sid)
                    if row is None or row["status"] != "ACTIVE":
                        with self._lock:
                            if self._load_locks.get(sid) is lk:
                                self._load_locks.pop(sid, None)
                        raise PlaygroundError(404, "Your sandbox expired. Start a new one.")
                    sb = Sandbox(row, self.registry.members(sid), db_path=self.cfg.dir / f"{sid}.db",
                                 base_cfg=self.base_cfg, client=self._client_for(row["chat_mode"]),
                                 paypal=self._paypal(sid), policy=self.policy, catalog=self.catalog,
                                 on_group_ready=lambda: self.registry.mark_group_ready(sid))
                    with self._lock:
                        self._cache[sid] = sb
                        self._inuse[sid] += 1
                        self._evict()
        try:
            yield sb
        finally:
            with self._lock:
                self._inuse[sid] -= 1
                if self._inuse[sid] <= 0:
                    del self._inuse[sid]
                self._evict()

    def _evict(self) -> None:
        """Close least recently used sandboxes beyond the cache size (never one in use)."""
        over = len(self._cache) - self.cfg.cache_size
        for sid in list(self._cache):
            if over <= 0:
                break
            if self._inuse.get(sid):
                continue
            self._cache.pop(sid).close()
            over -= 1

    def _drop(self, sid: str) -> bool:
        """Close and forget an open sandbox (caller holds its load lock). False if it is in use."""
        with self._lock:
            if self._inuse.get(sid):
                return False
            sb = self._cache.pop(sid, None)
        if sb is not None:
            sb.close()
        return True

    # ---- sessions -------------------------------------------------------------------
    def resolve(self, secret: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        if not secret or len(secret) > 128:
            raise PlaygroundError(401, "No sandbox session (X-Try-Session). Start a sandbox first.")
        found = self.registry.by_session(secret)
        if found is None:
            raise PlaygroundError(401, "Unknown sandbox session. Start a new sandbox.")
        row, member = found
        if not self._alive(row):          # before touch(), so an idle session cannot revive its sandbox
            raise self._gone(row)
        self.registry.touch(row["id"])
        return row, member

    def _start_payload(self, row, member, secret) -> dict[str, Any]:
        return {"secret": secret, "sandbox_id": row["id"], "chat_mode": row["chat_mode"],
                "member": {"user_id": member["user_id"], "name": member["name"], "role": member["role"]},
                "invite_url": self.invite_url(row["id"])}

    # ---- create / join ----------------------------------------------------------------
    def create(self, ip: str, name: str | None) -> dict[str, Any]:
        self.sweep()
        self._limit(self.create_limiter, ip, "new sandboxes")
        if self.registry.count_active() >= self.cfg.max_active:
            raise PlaygroundError(503, "The playground is at capacity right now. Please try again later.")
        sid, uid, display = secrets.token_hex(6), new_user_id(), clean_name(name)
        gid = f"{GROUP_PREFIX}{sid}"
        chat_mode = "mock"
        if self.live and self.registry.reserve_live_seat(self.cfg.max_live_users):
            registered = False
            try:
                self._register(uid, display)
                registered = True
                self.live_client.ensure_group(gid, f"GuardRail sandbox · {display}"[:64], self.base_cfg.bot_user_id,
                                              [self.base_cfg.bot_user_id, uid])
                chat_mode = "live"
            except NexconnError as e:
                if not registered:
                    self.registry.release_live_seat()
                if not is_capacity_error(e):
                    log.warning("playground: Nexconn registration failed: %s", e)
                    raise PlaygroundError(502, "Nexconn didn't answer, so no sandbox was created. Try again "
                                               "in a moment.") from None
                log.warning("playground: Nexconn capacity reached (%s); using the offline stand-in", e)
        secret = secrets.token_urlsafe(32)
        owner = {"user_id": uid, "name": display}
        if not self.registry.create(sid, gid, display, chat_mode, owner, secret, self.cfg.max_active):
            raise PlaygroundError(503, "The playground is at capacity right now. Please try again later.")
        if chat_mode == "live":
            self.registry.mark_group_ready(sid)
        with self.use(sid) as sb:
            sb.channel.post("welcome", f"Welcome to your GuardRail sandbox, {display}! Ask the agent for "
                                       "something in the guide. Anything over the limit shows up here as an "
                                       "approval card: approve or reject it right in this chat.")
        return self._start_payload(self.registry.get(sid), {**owner, "role": "owner"}, secret)

    def join(self, ip: str, code: str, name: str | None) -> dict[str, Any]:
        self.sweep()
        self._limit(self.join_limiter, ip, "joins")
        row = self.registry.by_invite(code) if isinstance(code, str) and 0 < len(code) <= 128 else None
        if not self._alive(row):
            if row is not None:
                self._gone(row)
            raise PlaygroundError(404, "This invite link is unknown or its sandbox expired.")
        sid = row["id"]
        if self.registry.member_count(sid) >= self.cfg.max_members:
            raise PlaygroundError(409, f"This sandbox already has {self.cfg.max_members} members.")
        uid, display = new_user_id(), clean_name(name)
        secret = secrets.token_urlsafe(32)
        try:   # take the member slot first, so nobody is registered on Nexconn for a full room
            self.registry.add_member(sid, {"user_id": uid, "name": display}, secret, self.cfg.max_members)
        except MembersFull:
            raise PlaygroundError(409, f"This sandbox already has {self.cfg.max_members} members.") from None
        if row["chat_mode"] == "live":
            if not self.registry.reserve_live_seat(self.cfg.max_live_users):
                self.registry.remove_member(uid)
                raise PlaygroundError(503, "Live Nexconn seats are full right now, so no teammate can join this "
                                           "sandbox. Start your own sandbox instead (it runs on an offline stand-in).")
            try:
                self._register(uid, display)
            except NexconnError as e:
                self.registry.release_live_seat()
                self.registry.remove_member(uid)
                log.warning("playground: teammate registration failed: %s", e)
                raise PlaygroundError(502, "Nexconn didn't answer; try joining again in a moment.") from None
        self.registry.touch(sid, 0)
        with self.use(sid) as sb:
            try:
                sb.channel.add_member(uid, display)
            except NexconnError as e:        # retried by the next bootstrap (session / post)
                log.warning("playground: adding %s to %s failed: %s", uid, sb.group_id, e)
                sb.channel._group_ready, sb.channel.ready = False, False
            sb.channel.post("joined", f"{display} joined as an approver.")
        return self._start_payload(row, {"user_id": uid, "name": display, "role": "teammate"}, secret)

    # ---- per-session actions ------------------------------------------------------------
    def state(self, secret: str | None) -> dict[str, Any]:
        self.sweep()
        row, member = self.resolve(secret)
        sid = row["id"]
        members = self.registry.members(sid)
        with self.use(sid) as sb:
            if self.clock() - sb.last_retry > RETRY_EVERY_S:   # re-send room posts that failed
                sb.last_retry = self.clock()
                sb.channel.retry_failed(auto=True)
            out = sb.summary()
        out["steps"].update({"joined": bool(row["room_joined"]), "invited": len(members) > 1})
        return {"sandbox_id": sid, "name": row["name"], "chat_mode": row["chat_mode"],
                "chat_fallback": row["chat_mode"] == "mock" and self.live,
                "you": {"user_id": member["user_id"], "name": member["name"], "role": member["role"]},
                "members": [{"user_id": m["user_id"], "name": m["name"], "role": m["role"]} for m in members],
                "max_members": self.cfg.max_members, "invite_url": self.invite_url(sid),
                "expires_at": _iso(max(row["last_seen"], self.clock()) + self.cfg.ttl_s), **out}

    def session(self, secret: str | None) -> dict[str, Any]:
        row, member = self.resolve(secret)
        with self.use(row["id"]) as sb:
            try:
                if member["user_id"] not in sb.channel.cfg.approver_ids:   # joined while this copy was open
                    sb.channel.add_member(member["user_id"], member["name"])
                s = sb.channel.session(member["user_id"])
            except PermissionError:
                raise PlaygroundError(403, "You are not a member of this sandbox.") from None
            except NexconnError as e:
                log.warning("playground session for %s failed: %s", row["id"], e)
                raise PlaygroundError(502, "Nexconn didn't answer; try again in a moment.") from None
        self.registry.mark_room_joined(row["id"])
        return s

    def agent(self, ip: str, secret: str | None, request: str) -> dict[str, Any]:
        row, _ = self.resolve(secret)
        self._limit(self.agent_ip_limiter, ip, "agent requests")
        self._limit(self.agent_limiter, row["id"], "agent requests in a sandbox")
        with self.use(row["id"]) as sb:
            res = sb.agent.run(request).to_dict()
            text = sb.readable(res["final_text"])
        return {"final_text": text, "request_id": res["request_id"], "pay_links": Sandbox.pay_links(res)}

    def reset(self, secret: str | None) -> dict[str, Any]:
        row, member = self.resolve(secret)
        if member["role"] != "owner":
            raise PlaygroundError(403, "Only the sandbox owner can reset it.")
        sid = row["id"]
        with self._lock:
            lk = self._load_locks.setdefault(sid, threading.Lock())
        with lk:
            if not self._drop(sid):
                raise PlaygroundError(409, "The sandbox is busy; try the reset again in a moment.")
            delete_sandbox_files(self.cfg.dir, sid)
            with self._lock:
                self._paypals.pop(sid, None)
        with self.use(sid) as sb:
            sb.channel.post("reset", "Sandbox was reset: orders, approvals and today's spend are cleared "
                                     "(fresh daily cap). Earlier cards in this room are closed.", {"reset": True})
        return {"ok": True}

    # ---- room messages --------------------------------------------------------------------
    @staticmethod
    def owns(item: dict[str, Any]) -> bool:
        gid = item.get("channelId")
        return isinstance(gid, str) and gid.startswith(GROUP_PREFIX)

    def handle_message(self, item: dict[str, Any], source: str) -> dict[str, Any]:
        """A message from a playground room (signed webhook in live mode, or the mock transport)."""
        gid = item.get("channelId")
        row = self.registry.by_group(gid) if isinstance(gid, str) else None
        if not self._alive(row):
            if row is not None:
                self._gone(row)
            return {"result": "IGNORED", "why": "unknown or expired sandbox room"}
        try:
            with self.use(row["id"]) as sb:
                return sb.channel.handle_message(item, source)
        except PlaygroundError:
            return {"result": "IGNORED", "why": "unknown or expired sandbox room"}

    def _mock_member(self, token: str | None) -> tuple[dict[str, Any], str]:
        uid = self.mock.user_for_token(token or "") if token else None
        if uid is None:
            raise PlaygroundError(401, "Room session expired; reconnecting.")
        with self.registry.lock:
            m = self.registry.conn.execute("SELECT sandbox_id FROM members WHERE user_id=?", (uid,)).fetchone()
        row = self.registry.get(m["sandbox_id"]) if m else None
        if not self._alive(row) or row["chat_mode"] != "mock":
            if row is not None and row["status"] == "ACTIVE" and row["chat_mode"] == "mock":
                self._gone(row)
            raise PlaygroundError(401, "Room session expired; reconnecting.")
        return row, uid

    def mock_messages(self, token: str | None, after: int) -> dict[str, Any]:
        row, _ = self._mock_member(token)
        after = max(0, min(after, 1_000_000))
        msgs = self.mock.history(row["group_id"], after)
        return {"messages": msgs, "next": after + len(msgs)}

    def mock_send(self, token: str | None, text: str, extra: dict | None) -> dict[str, Any]:
        row, uid = self._mock_member(token)
        self._limit(self.mock_send_limiter, row["id"], "room messages in a sandbox")
        self.registry.touch(row["id"])
        extra = _clean_extra(extra)
        with self.use(row["id"]) as sb:
            sb.channel.bootstrap()
            try:
                mid = self.mock.send_group_text(uid, row["group_id"], text, extra)
            except NexconnError as e:
                if e.status == 429:
                    raise PlaygroundError(429, "This sandbox room is full. Start a new sandbox to keep "
                                               "trying things.") from None
                raise PlaygroundError(502, str(e)) from None
            return {"message_id": mid,
                    "handled": sb.channel.handle_message(_item(row["group_id"], uid, mid, text, extra), "mock")}

    # ---- simulated checkout ----------------------------------------------------------------
    def checkout_limit(self, ip: str) -> None:
        self._limit(self.checkout_limiter, ip, "checkout requests")

    def checkout_order(self, sid: str, token: str) -> dict[str, Any]:
        with self.use(sid) as sb:
            try:
                return sb.paypal.get_order(token)["raw"]
            except PayPalError:
                raise PlaygroundError(404, "Unknown checkout (simulated orders are lost when the server "
                                           "restarts; ask the agent again).") from None

    def checkout_approve(self, sid: str, token: str) -> None:
        with self.use(sid) as sb:
            try:
                sb.paypal.approve(token)
            except PayPalError:
                raise PlaygroundError(404, "Unknown checkout.") from None

    def checkout_return(self, sid: str, token: str) -> dict[str, Any]:
        with self.use(sid) as sb:
            try:
                return sb.svc.complete_payment(token)
            except KeyError:
                raise PlaygroundError(404, "Unknown checkout.") from None
            except ValueError as e:
                raise PlaygroundError(409, str(e)) from None

    def checkout_cancel(self, sid: str, token: str, state: str) -> dict[str, Any]:
        with self.use(sid) as sb:
            try:
                return sb.svc.cancel_payment(token, state)
            except KeyError:
                raise PlaygroundError(404, "Unknown checkout.") from None
            except PermissionError:
                raise PlaygroundError(403, "Missing or invalid signed cancel state.") from None

    # ---- lifecycle ----------------------------------------------------------------------------
    def sweep(self, force: bool = False) -> list[str]:
        """Expire sandboxes idle longer than TRY_TTL_HOURS (at most once per 10 minutes)."""
        now = self.clock()
        with self._lock:
            if not force and now - self._last_sweep < SWEEP_EVERY_S:
                return []
            self._last_sweep = now
        done = []
        for sid in self.registry.idle(now - self.cfg.ttl_s):
            try:
                if self.expire(sid):
                    done.append(sid)
            except Exception:   # one broken sandbox must not stop the sweep
                log.exception("playground: expiring %s failed", sid)
        return done

    def expire(self, sid: str) -> bool:
        with self._lock:
            lk = self._load_locks.setdefault(sid, threading.Lock())
        with lk:
            if not self._drop(sid):
                return False                # in use right now: the next sweep gets it
            if not self.registry.mark_expired(sid):
                return False
            delete_sandbox_files(self.cfg.dir, sid)
            row = self.registry.get(sid)
            if row is not None:
                self.mock.drop_group(row["group_id"])
            for m in self.registry.members(sid):
                self.mock.revoke_user(m["user_id"])
            with self._lock:
                self._paypals.pop(sid, None)
                self._load_locks.pop(sid, None)
        log.info("playground: sandbox %s expired", sid)
        return True

    def health(self) -> dict[str, Any]:
        live_left = self.live_seats_left()
        return {"mode": "on", "active_sandboxes": self.registry.count_active(),
                "live_seats_left": live_left if self.live else None,
                "chat_fallback": (not self.live) or live_left == 0}

    def ready(self) -> bool:
        return self.registry.ping()

    def close(self) -> None:
        self.closed = True
        with self._lock:
            open_ = list(self._cache.values())
            self._cache.clear()
        for sb in open_:
            try:
                sb.close()
            except Exception:  # pragma: no cover - best effort at shutdown
                log.exception("closing sandbox %s failed", sb.id)
        self.registry.close()


def default_dir() -> Path:
    return Path(os.environ.get("TRY_DIR") or PlaygroundConfig().dir)
