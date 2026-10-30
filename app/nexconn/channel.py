"""The chat approval channel: approval requests go to a Nexconn group, decisions come back.

    agent asks for approval ──► BuyerService.request_approval ──► listener ──► card in the room
    approver replies "approve APR-…" in the room ──► webhook (live) / mock relay ──►
        handle_message: right room? approver? not seen before? ──► BuyerService.approve/reject
        (the same server-side path as the dashboard: hard rules re-checked, quote bound,
        claimed exactly once) ──► listener ──► outcome posted back into the room

Decisions made on the dashboard are posted to the room too, so the room mirrors every
approval request and its outcome (the audit log in the database stays the system of record).
Posting is best effort (at least once) and never blocks or changes a purchase: a failed post
is recorded (FAILED) and retried; abandoned posts and half-done decisions are recovered.
"""
from __future__ import annotations

import contextvars
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .client import NexconnError
from .config import NexconnConfig
from .protocol import Decision, outcome_text, parse_decision, refusal_text, request_card

log = logging.getLogger("guardrail.nexconn")

# Who is deciding right now (set by handle_message around svc.approve / svc.reject, read by
# the approval listener in the same thread). Unset means the dashboard / REST API decided.
_DECIDER: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar(
    "nexconn_decider", default=None)

# A claim (post being sent, inbound message being handled, webhook nonce being processed) older
# than this is treated as abandoned (process died mid-way) and may be taken over.
STALE_CLAIM_S = 120
RECOVER_EVERY_S = 30      # the poster thread looks for abandoned work this often when idle
AUTO_RETRY_MAX = 5        # automatic re-sends of a FAILED post (POST /nexconn/retry has no cap)


class InboundBusy(RuntimeError):
    """The same message is being handled right now by another request; retry later."""

_SCHEMA = (
    # outbox: QUEUED -> SENDING (claimed) -> SENT | FAILED
    """CREATE TABLE IF NOT EXISTS nexconn_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        approval_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, extra TEXT NOT NULL,
        status TEXT NOT NULL, message_id TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        claimed_at REAL, created_at TEXT NOT NULL, sent_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS nexconn_posts_status ON nexconn_posts(status)",
    # inbound messages: PROCESSING (claimed) -> DONE; a failed attempt deletes its claim
    """CREATE TABLE IF NOT EXISTS nexconn_inbound (
        message_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, approval_id TEXT, decision TEXT,
        outcome TEXT, source TEXT NOT NULL, state TEXT NOT NULL, claimed_at REAL NOT NULL,
        received_at TEXT NOT NULL)""",
    # webhook nonces (durable, shared by every worker process on this DB): PROCESSING -> DONE
    """CREATE TABLE IF NOT EXISTS nexconn_nonces (
        nonce TEXT PRIMARY KEY, state TEXT NOT NULL, claimed_at REAL NOT NULL)""",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class ChatApprovalChannel:
    def __init__(self, svc, cfg: NexconnConfig, client, dispatch: str = "thread"):
        if dispatch not in ("thread", "inline"):
            raise ValueError("dispatch must be 'thread' or 'inline'")
        self.svc, self.cfg, self.client = svc, cfg, client
        self.dispatch = dispatch
        self.ready = False
        self.last_error: str | None = None
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._boot_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._clock = time.time
        with svc.store.tx() as c:
            for stmt in _SCHEMA:
                c.execute(stmt)
        svc.add_approval_listener(self._on_approval)

    # ---- setup ----------------------------------------------------------------
    def bootstrap(self) -> bool:
        """Register the bot and approvers and make sure the approval group exists with all of
        them in it. Safe to call again; failures are kept in last_error and retried lazily."""
        with self._boot_lock:
            if self.ready:
                return True
            try:
                self.client.issue_token(self.cfg.bot_user_id, "GuardRail agent")  # registers the bot
                for a in self.cfg.approver_ids:
                    self.client.issue_token(a, a)
                self.client.ensure_group(self.cfg.group_id, self.cfg.group_name, self.cfg.bot_user_id,
                                         [self.cfg.bot_user_id, *self.cfg.approver_ids])
            except NexconnError as e:
                self.last_error = f"bootstrap: {e}"
                log.warning("Nexconn bootstrap failed: %s", e)
                return False
            self.ready, self.last_error = True, None
            return True

    def start(self) -> None:
        """Startup: bootstrap, re-send posts a previous run left unsent, finish decisions it
        left half-done, and (threaded) start the poster thread that keeps doing so."""
        self.bootstrap()
        self.recover()
        if self.dispatch == "thread":
            self._ensure_worker()

    def recover(self, auto: bool = False) -> dict[str, int]:
        decisions = self.recover_inbound()
        outcomes = self.recover_outcomes()
        return {"posts": self.retry_failed(auto=auto), "decisions": decisions, "outcomes": outcomes}

    def recover_outcomes(self) -> int:
        """Post the outcome of approvals that were carded in the room and decided (anywhere) but
        whose outcome was never queued, e.g. the process died between the decision's commit and
        its listener. Rebuilt from the stored approval / order, so it is the committed outcome."""
        st = self.svc.store
        cutoff = _iso(self._clock() - STALE_CLAIM_S)   # leave a just-decided approval to its listener
        with st.lock:
            rows = [dict(r) for r in st.conn.execute(
                "SELECT a.id, a.status, a.order_id, a.decided_at FROM approvals a "
                "WHERE a.status NOT IN ('PENDING','EXECUTING') AND a.decided_at IS NOT NULL AND a.decided_at < ? "
                "AND EXISTS (SELECT 1 FROM nexconn_posts p WHERE p.approval_id=a.id AND p.kind='request') "
                "AND NOT EXISTS (SELECT 1 FROM nexconn_posts p WHERE p.approval_id=a.id AND p.kind='outcome')",
                (cutoff,)).fetchall()]
            # who decided in chat: the first handled chat decision of the matching kind (a recovered
            # one may read NOT_PENDING because its own earlier attempt had already committed it)
            deciders: dict[str, str] = {}
            for r in st.conn.execute(
                    "SELECT approval_id, user_id, decision FROM nexconn_inbound WHERE state='DONE' AND outcome NOT IN "
                    "('NOT_AN_APPROVER','UNKNOWN_APPROVAL') ORDER BY received_at, rowid"):
                deciders.setdefault((r["approval_id"], r["decision"]), r["user_id"])
        for a in rows:
            result: dict[str, Any] = {"result": {"REJECTED": "REJECTED", "DENIED_BY_POLICY": "DENIED",
                                                 "QUOTE_CHANGED": "QUOTE_CHANGED"}.get(a["status"], a["status"])}
            if a["status"] == "APPROVED" and a["order_id"]:
                order = st.get("orders", a["order_id"])
                if order:
                    result = {"result": self.svc._result(order), "order": order}
            by = deciders.get((a["id"], "reject" if a["status"] == "REJECTED" else "approve"))
            text, extra = outcome_text(a["id"], result, "chat" if by else "dashboard", by)
            self._enqueue(a["id"], "outcome", text, extra)
        return len(rows)

    def session(self, user_id: str | None) -> dict[str, Any]:
        """A Chat SDK session for an approver (the route requires the approver token)."""
        uid = user_id or self.cfg.approver_ids[0]
        if uid not in self.cfg.approver_ids:
            raise PermissionError(f"{uid} is not in NEXCONN_APPROVER_IDS")
        self.bootstrap()
        token = self.client.issue_token(uid, uid)
        return {"mode": self.cfg.mode, "app_key": self.cfg.app_key if self.cfg.mode == "live" else "",
                "area": self.cfg.area, "token": token, "user_id": uid, "group_id": self.cfg.group_id,
                "bot_user_id": self.cfg.bot_user_id, "approver_ids": list(self.cfg.approver_ids)}

    # ---- outbound ---------------------------------------------------------------
    def _on_approval(self, event: str, approval: dict[str, Any] | None, result: dict[str, Any]) -> None:
        if not approval:
            return
        aid = approval["id"]
        if event == "requested":
            text, extra = request_card(approval)
            self._enqueue(aid, "request", text, extra)
        elif event == "decided":
            via, by = _DECIDER.get() or ("dashboard", None)
            text, extra = outcome_text(aid, result, via, by)
            self._enqueue(aid, "outcome", text, extra)

    def notice(self, approval_id: str, why: str) -> None:
        text, extra = refusal_text(approval_id, why)
        self._enqueue(approval_id, "notice", text, extra)

    def _enqueue(self, approval_id: str, kind: str, text: str, extra: dict) -> None:
        with self.svc.store.tx() as c:
            cur = c.execute("INSERT INTO nexconn_posts (approval_id, kind, text, extra, status, created_at) "
                            "VALUES (?,?,?,?, 'QUEUED', ?)", (approval_id, kind, text, json.dumps(extra), _now()))
            post_id = cur.lastrowid
        if self.dispatch == "inline":
            self._send(post_id)
        else:
            self._ensure_worker()
            self._q.put(post_id)

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="nexconn-poster", daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while True:
            try:
                post_id = self._q.get(timeout=RECOVER_EVERY_S)
            except queue.Empty:        # idle: pick up work a crashed or restarted process left behind
                try:
                    self.recover(auto=True)
                except Exception:  # pragma: no cover - recovery is best effort
                    log.exception("nexconn recovery failed")
                continue
            try:
                if post_id is not None:
                    self._send(post_id)
            except Exception:  # pragma: no cover - _send records its own errors
                log.exception("nexconn post %s crashed", post_id)
            finally:
                self._q.task_done()

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait until queued posts are sent (tests, shutdown). True if the queue emptied."""
        if self.dispatch == "inline" or self._worker is None:
            return True
        done = threading.Event()
        threading.Thread(target=lambda: (self._q.join(), done.set()), daemon=True).start()
        return done.wait(timeout)

    def _send(self, post_id: int) -> None:
        st = self.svc.store
        now = self._clock()
        with st.tx() as c:   # claim: only one worker / process sends a given post
            claimed = c.execute(
                "UPDATE nexconn_posts SET status='SENDING', claimed_at=? WHERE id=? AND "
                "(status IN ('QUEUED','FAILED') OR (status='SENDING' AND claimed_at < ?))",
                (now, post_id, now - STALE_CLAIM_S)).rowcount
            row = c.execute("SELECT * FROM nexconn_posts WHERE id=?", (post_id,)).fetchone()
        if claimed != 1 or row is None:
            return
        try:
            if not self.bootstrap():
                raise NexconnError(self.last_error or "bootstrap failed")
            mid = self.client.send_group_text(self.cfg.bot_user_id, self.cfg.group_id, row["text"],
                                              json.loads(row["extra"]))
        except NexconnError as e:
            with st.tx() as c:
                c.execute("UPDATE nexconn_posts SET status='FAILED', error=?, attempts=attempts+1 WHERE id=?",
                          (str(e)[:300], post_id))
            log.warning("Nexconn post %s (%s) failed: %s", post_id, row["kind"], e)
            return
        with st.tx() as c:
            c.execute("UPDATE nexconn_posts SET status='SENT', message_id=?, error=NULL, attempts=attempts+1, "
                      "sent_at=? WHERE id=?", (mid, _now(), post_id))

    def retry_failed(self, auto: bool = False) -> int:
        """Re-send posts that failed, were queued but never sent (e.g. the process stopped),
        or whose sender died mid-send (at-least-once: such a post may appear twice). Called at
        startup, periodically by the poster thread (auto: FAILED only up to AUTO_RETRY_MAX
        attempts, QUEUED only once stale) and by POST /nexconn/retry."""
        st = self.svc.store
        stale = self._clock() - STALE_CLAIM_S
        with st.lock:
            if auto:
                rows = st.conn.execute(
                    "SELECT id FROM nexconn_posts WHERE (status='FAILED' AND attempts < ?) OR "
                    "(status IN ('QUEUED','SENDING') AND COALESCE(claimed_at, 0) < ? AND created_at < ?) "
                    "ORDER BY id", (AUTO_RETRY_MAX, stale, _iso(stale))).fetchall()
            else:
                rows = st.conn.execute(
                    "SELECT id FROM nexconn_posts WHERE status IN ('FAILED','QUEUED') OR "
                    "(status='SENDING' AND claimed_at < ?) ORDER BY id", (stale,)).fetchall()
            ids = [r["id"] for r in rows]
        for pid in ids:
            if self.dispatch == "inline":
                self._send(pid)
            else:
                self._ensure_worker()
                self._q.put(pid)
        return len(ids)

    # ---- inbound ----------------------------------------------------------------
    def handle_message(self, item: dict[str, Any], source: str) -> dict[str, Any]:
        """One message from the room (a `message:send` webhook item, or the mock relay).
        Returns what happened; never raises for bad input."""
        uid, mid = item.get("userId"), item.get("messageId")
        if not isinstance(uid, str) or not isinstance(mid, str) or not mid:
            return {"result": "IGNORED", "why": "malformed"}
        if item.get("channelId") != self.cfg.group_id or item.get("channelType") not in (3, "3", None):
            return {"result": "IGNORED", "why": "other channel"}
        if uid == self.cfg.bot_user_id:
            return {"result": "IGNORED", "why": "own message"}
        decision = parse_decision(str(item.get("messageType") or ""), item.get("content"))
        if decision is None:
            return {"result": "IGNORED", "why": "not a decision"}
        claim = self._claim_inbound(mid, uid, decision.approval_id, decision.decision, source)
        if claim == "done":
            return {"result": "DUPLICATE", "message_id": mid}
        if claim == "busy":       # in progress elsewhere: not done yet, so the caller must not ack it
            raise InboundBusy(mid)
        try:
            return self._decide(mid, uid, decision)
        except BaseException:
            # unexpected failure: drop the claim so a webhook retry (or the relay) can redo it;
            # BuyerService.approve/reject are safe to repeat (claimed once, replays return the order)
            with self.svc.store.tx() as c:
                c.execute("DELETE FROM nexconn_inbound WHERE message_id=? AND state='PROCESSING'", (mid,))
            raise

    def _claim_inbound(self, mid: str, uid: str, approval_id: str, decision: str, source: str) -> str:
        """'new' (claimed), 'done' (already handled) or 'busy' (being handled right now)."""
        now = self._clock()
        with self.svc.store.tx() as c:
            if c.execute("INSERT OR IGNORE INTO nexconn_inbound (message_id, user_id, approval_id, decision, source, "
                         "state, claimed_at, received_at) VALUES (?,?,?,?,?, 'PROCESSING', ?, ?)",
                         (mid, uid, approval_id, decision, source, now, _now())).rowcount == 1:
                return "new"
            # taken over only if a previous handler died mid-way
            if c.execute("UPDATE nexconn_inbound SET claimed_at=?, source=? WHERE message_id=? AND "
                         "state='PROCESSING' AND claimed_at < ?",
                         (now, source, mid, now - STALE_CLAIM_S)).rowcount == 1:
                return "new"
            row = c.execute("SELECT state FROM nexconn_inbound WHERE message_id=?", (mid,)).fetchone()
            return "done" if row and row["state"] == "DONE" else "busy"

    def recover_inbound(self) -> int:
        """Finish decisions whose handler died mid-way (claim PROCESSING and stale): the claim row
        holds the message id, sender, approval id and decision, so no redelivery is needed."""
        st = self.svc.store
        now = self._clock()
        with st.lock:
            rows = [dict(r) for r in st.conn.execute(
                "SELECT message_id, user_id, approval_id, decision, source FROM nexconn_inbound "
                "WHERE state='PROCESSING' AND claimed_at < ?", (now - STALE_CLAIM_S,)).fetchall()]
        done = 0
        for r in rows:
            if self._claim_inbound(r["message_id"], r["user_id"], r["approval_id"], r["decision"],
                                   r["source"]) != "new":
                continue
            try:
                self._decide(r["message_id"], r["user_id"], Decision(r["approval_id"], r["decision"]),
                             recovering=True)
                done += 1
            except Exception:
                log.exception("recovering decision %s failed", r["message_id"])
        return done

    def _decide(self, mid: str, uid: str, decision, recovering: bool = False) -> dict[str, Any]:
        """recovering: re-running a claim whose handler died; the decision may already have been
        committed by that handler, so "already decided" is not news worth posting."""
        if uid not in self.cfg.approver_ids:
            self._record(mid, "NOT_AN_APPROVER")
            self.notice(decision.approval_id, f"{uid} is not an approver; nothing changed")
            return {"result": "NOT_AN_APPROVER", "user_id": uid}
        fn: Callable[[str], dict] = self.svc.approve if decision.decision == "approve" else self.svc.reject
        token = _DECIDER.set(("chat", uid))
        try:
            out = fn(decision.approval_id)
        except KeyError:
            self._record(mid, "UNKNOWN_APPROVAL")
            self.notice(decision.approval_id, "no such approval request")
            return {"result": "UNKNOWN_APPROVAL"}
        except ValueError as e:              # already decided / expired / claimed elsewhere
            self._record(mid, "NOT_PENDING")
            if not recovering:
                self.notice(decision.approval_id, f"{e}; nothing changed")
            return {"result": "NOT_PENDING", "detail": str(e)}
        finally:
            _DECIDER.reset(token)
        res = str(out.get("result"))
        self._record(mid, res)
        if out.get("replayed"):
            if not recovering:
                self.notice(decision.approval_id, "was already approved; nothing changed")
        return {"result": res, "approval_id": decision.approval_id, "decision": decision.decision,
                "decided_by": uid, **({"order_id": out["order"]["id"]} if isinstance(out.get("order"), dict) else {})}

    def _record(self, message_id: str, outcome: str) -> None:
        with self.svc.store.tx() as c:
            c.execute("UPDATE nexconn_inbound SET outcome=?, state='DONE' WHERE message_id=?", (outcome, message_id))

    # ---- webhook nonces (durable: every worker process on this DB sees them) -----
    def claim_nonce(self, nonce: str, window_s: float) -> str:
        """'new' (claimed for processing), 'done' (a handled delivery: answer 200, do nothing)
        or 'busy' (being processed right now: answer non-2xx so Nexconn retries later)."""
        now = self._clock()
        with self.svc.store.tx() as c:
            c.execute("DELETE FROM nexconn_nonces WHERE claimed_at < ?", (now - 2 * window_s - STALE_CLAIM_S,))
            if c.execute("INSERT OR IGNORE INTO nexconn_nonces (nonce, state, claimed_at) VALUES (?, 'PROCESSING', ?)",
                         (nonce, now)).rowcount == 1:
                return "new"
            row = c.execute("SELECT state, claimed_at FROM nexconn_nonces WHERE nonce=?", (nonce,)).fetchone()
            if row["state"] == "DONE":
                return "done"
            if row["claimed_at"] < now - STALE_CLAIM_S:
                c.execute("UPDATE nexconn_nonces SET claimed_at=? WHERE nonce=?", (now, nonce))
                return "new"
            return "busy"

    def finish_nonce(self, nonce: str, ok: bool) -> None:
        with self.svc.store.tx() as c:
            if ok:
                c.execute("UPDATE nexconn_nonces SET state='DONE' WHERE nonce=?", (nonce,))
            else:
                c.execute("DELETE FROM nexconn_nonces WHERE nonce=? AND state='PROCESSING'", (nonce,))

    # ---- views ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        st = self.svc.store
        with st.lock:
            posts = [dict(r) for r in st.conn.execute(
                "SELECT id, approval_id, kind, status, message_id, error, attempts, created_at, sent_at "
                "FROM nexconn_posts ORDER BY id DESC LIMIT 200").fetchall()]
            inbound = [dict(r) for r in st.conn.execute(
                "SELECT message_id, user_id, approval_id, decision, outcome, source, state, received_at "
                "FROM nexconn_inbound ORDER BY received_at DESC, rowid DESC LIMIT 200").fetchall()]
        return {**self.cfg.public(), "ready": self.ready, "last_error": self.last_error,
                "client": getattr(self.client, "name", type(self.client).__name__),
                "posts": posts, "inbound": inbound}
