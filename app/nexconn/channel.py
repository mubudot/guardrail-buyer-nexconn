"""The chat approval channel: approval requests go to a Nexconn group, decisions come back.

    agent asks for approval ──► BuyerService.request_approval ──► listener ──► card in the room
    approver replies "approve APR-…" in the room ──► webhook (live) / mock relay ──►
        handle_message: right room? approver? not seen before? ──► BuyerService.approve/reject
        (the same server-side path as the dashboard: hard rules re-checked, quote bound,
        claimed exactly once) ──► listener ──► outcome posted back into the room

Decisions made on the dashboard are posted to the room too, so the room is a complete
audit trail of every approval. Posting is best effort and never blocks or changes a
purchase: a failed post is recorded (status FAILED) and retried by `retry_failed()`.
"""
from __future__ import annotations

import contextvars
import json
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .client import NexconnError
from .config import NexconnConfig
from .protocol import outcome_text, parse_decision, refusal_text, request_card

log = logging.getLogger("guardrail.nexconn")

# Who is deciding right now (set by handle_message around svc.approve / svc.reject, read by
# the approval listener in the same thread). Unset means the dashboard / REST API decided.
_DECIDER: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar(
    "nexconn_decider", default=None)

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS nexconn_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        approval_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, extra TEXT NOT NULL,
        status TEXT NOT NULL, message_id TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, sent_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS nexconn_posts_approval ON nexconn_posts(approval_id)",
    """CREATE TABLE IF NOT EXISTS nexconn_inbound (
        message_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, approval_id TEXT, decision TEXT,
        outcome TEXT, source TEXT NOT NULL, received_at TEXT NOT NULL)""",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="nexconn-poster", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while True:
            post_id = self._q.get()
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
        with st.lock:
            row = st.conn.execute("SELECT * FROM nexconn_posts WHERE id=?", (post_id,)).fetchone()
        if row is None or row["status"] == "SENT":
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

    def retry_failed(self) -> int:
        st = self.svc.store
        with st.lock:
            ids = [r["id"] for r in st.conn.execute(
                "SELECT id FROM nexconn_posts WHERE status='FAILED' ORDER BY id").fetchall()]
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
        st = self.svc.store
        with st.tx() as c:
            fresh = c.execute("INSERT OR IGNORE INTO nexconn_inbound (message_id, user_id, approval_id, decision, "
                              "source, received_at) VALUES (?,?,?,?,?,?)",
                              (mid, uid, decision.approval_id, decision.decision, source, _now())).rowcount
        if fresh != 1:
            return {"result": "DUPLICATE", "message_id": mid}
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
            self.notice(decision.approval_id, f"{e}; nothing changed")
            return {"result": "NOT_PENDING", "detail": str(e)}
        finally:
            _DECIDER.reset(token)
        res = str(out.get("result"))
        self._record(mid, res)
        if out.get("replayed"):
            self.notice(decision.approval_id, "was already approved; nothing changed")
        return {"result": res, "approval_id": decision.approval_id, "decision": decision.decision,
                "decided_by": uid, **({"order_id": out["order"]["id"]} if isinstance(out.get("order"), dict) else {})}

    def _record(self, message_id: str, outcome: str) -> None:
        with self.svc.store.tx() as c:
            c.execute("UPDATE nexconn_inbound SET outcome=? WHERE message_id=?", (outcome, message_id))

    # ---- views ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        st = self.svc.store
        with st.lock:
            posts = [dict(r) for r in st.conn.execute(
                "SELECT id, approval_id, kind, status, message_id, error, attempts, created_at, sent_at "
                "FROM nexconn_posts ORDER BY id DESC LIMIT 200").fetchall()]
            inbound = [dict(r) for r in st.conn.execute(
                "SELECT * FROM nexconn_inbound ORDER BY received_at DESC, rowid DESC LIMIT 200").fetchall()]
        return {**self.cfg.public(), "ready": self.ready, "last_error": self.last_error,
                "client": getattr(self.client, "name", type(self.client).__name__),
                "posts": posts, "inbound": inbound}
