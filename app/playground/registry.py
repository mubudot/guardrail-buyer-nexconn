"""The playground registry (TRY_DIR/registry.db): which sandboxes exist, who is in them, and the
persisted counters. Secrets are stored only as SHA-256 hashes.

    sandboxes  id, group_id, name, chat_mode (live|mock), invite_hash, status (ACTIVE|EXPIRED),
               created_at, last_seen, room_joined, group_ready
    members    user_id, sandbox_id, name, role (owner|teammate), session_hash, joined_at
    meta       counters (live_users_registered, ...) and the invite-code key

One connection, autocommit, explicit BEGIN IMMEDIATE transactions under a lock (same pattern as
app/store.py), WAL so the operator CLI can read it while the server runs.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sandboxes (
        id TEXT PRIMARY KEY, group_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, chat_mode TEXT NOT NULL,
        invite_hash TEXT NOT NULL UNIQUE, status TEXT NOT NULL, created_at REAL NOT NULL,
        last_seen REAL NOT NULL, room_joined REAL, group_ready INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS sandboxes_status ON sandboxes(status, last_seen)",
    """CREATE TABLE IF NOT EXISTS members (
        user_id TEXT PRIMARY KEY, sandbox_id TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
        session_hash TEXT NOT NULL UNIQUE, joined_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS members_sandbox ON members(sandbox_id)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class MembersFull(Exception):
    """The sandbox already has TRY_MAX_MEMBERS members."""


class Registry:
    def __init__(self, path: Path, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.tx() as c:
            for stmt in _SCHEMA:
                c.execute(stmt)
            # VPS registries predate fallback metadata. Check each column in the same write
            # transaction so opening the old DB (or opening it again) is safe.
            columns = {r[1] for r in c.execute("PRAGMA table_info(sandboxes)")}
            for name in ("fallback_reason", "fallback_since", "fallback_detail"):
                if name not in columns:
                    c.execute(f"ALTER TABLE sandboxes ADD COLUMN {name} TEXT")
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('invite_key', ?)", (secrets.token_hex(32),))
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('live_users_registered', '0')")
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('sandboxes_created', '0')")
        self._invite_key = self.meta("invite_key").encode()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def ping(self) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1").fetchone()[0] == 1

    # ---- meta / counters -------------------------------------------------------
    def meta(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def counter(self, key: str) -> int:
        return int(self.meta(key, "0") or 0)

    def reserve_live_seat(self, cap: int) -> bool:
        """Count one more Nexconn user registration if the lifetime cap allows it (atomic)."""
        with self.tx() as c:
            n = int(c.execute("SELECT value FROM meta WHERE key='live_users_registered'").fetchone()[0])
            if n >= cap:
                return False
            c.execute("UPDATE meta SET value=? WHERE key='live_users_registered'", (str(n + 1),))
            return True

    def release_live_seat(self) -> None:
        """Undo a reservation whose registration never happened."""
        with self.tx() as c:
            c.execute("UPDATE meta SET value=CAST(MAX(CAST(value AS INTEGER) - 1, 0) AS TEXT) "
                      "WHERE key='live_users_registered'")

    def invite_code(self, sandbox_id: str) -> str:
        """The sandbox's invite code: derived (HMAC with the registry's key), so the page can show it
        again after a reload while the table keeps only its hash."""
        return hmac.new(self._invite_key, f"invite:{sandbox_id}".encode(), hashlib.sha256).hexdigest()[:32]

    # ---- sandboxes ---------------------------------------------------------------
    def count_active(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM sandboxes WHERE status='ACTIVE'").fetchone()[0]

    def create(self, sandbox_id: str, group_id: str, name: str, chat_mode: str, owner: dict[str, str],
               session_secret: str, max_active: int, *, fallback_reason: str | None = None,
               fallback_since: str | None = None, fallback_detail: str | None = None) -> bool:
        """Insert the sandbox and its owner; False (nothing written) when max_active is reached."""
        now = self.clock()
        with self.tx() as c:
            if c.execute("SELECT COUNT(*) FROM sandboxes WHERE status='ACTIVE'").fetchone()[0] >= max_active:
                return False
            c.execute("INSERT INTO sandboxes (id, group_id, name, chat_mode, invite_hash, status, created_at, "
                      "last_seen, fallback_reason, fallback_since, fallback_detail) "
                      "VALUES (?,?,?,?,?, 'ACTIVE', ?, ?, ?, ?, ?)",
                      (sandbox_id, group_id, name, chat_mode, digest(self.invite_code(sandbox_id)), now, now,
                       fallback_reason, fallback_since, fallback_detail))
            c.execute("INSERT INTO members (user_id, sandbox_id, name, role, session_hash, joined_at) "
                      "VALUES (?,?,?, 'owner', ?, ?)", (owner["user_id"], sandbox_id, owner["name"],
                                                        digest(session_secret), now))
            c.execute("UPDATE meta SET value=CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='sandboxes_created'")
        return True

    def fall_back(self, sandbox_id: str, since: str, detail: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sandboxes SET chat_mode='mock', group_ready=0, fallback_reason='upstream', "
                      "fallback_since=?, fallback_detail=? WHERE id=? AND chat_mode='live' AND status='ACTIVE'",
                      (since, detail, sandbox_id))

    def get(self, sandbox_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,)).fetchone()
        return dict(row) if row else None

    def by_group(self, group_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM sandboxes WHERE group_id=?", (group_id,)).fetchone()
        return dict(row) if row else None

    def by_invite(self, code: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM sandboxes WHERE invite_hash=?", (digest(code),)).fetchone()
        return dict(row) if row else None

    def by_session(self, secret: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        with self.lock:
            m = self.conn.execute("SELECT * FROM members WHERE session_hash=?", (digest(secret),)).fetchone()
            if m is None:
                return None
            s = self.conn.execute("SELECT * FROM sandboxes WHERE id=?", (m["sandbox_id"],)).fetchone()
        return (dict(s), dict(m)) if s else None

    def members(self, sandbox_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT user_id, name, role, joined_at FROM members WHERE sandbox_id=? ORDER BY joined_at, rowid",
                (sandbox_id,)).fetchall()]

    def add_member(self, sandbox_id: str, member: dict[str, str], session_secret: str, max_members: int) -> None:
        with self.tx() as c:
            n = c.execute("SELECT COUNT(*) FROM members WHERE sandbox_id=?", (sandbox_id,)).fetchone()[0]
            if n >= max_members:
                raise MembersFull(sandbox_id)
            c.execute("INSERT INTO members (user_id, sandbox_id, name, role, session_hash, joined_at) "
                      "VALUES (?,?,?, 'teammate', ?, ?)", (member["user_id"], sandbox_id, member["name"],
                                                           digest(session_secret), self.clock()))

    def remove_member(self, user_id: str) -> None:
        """Undo add_member when the teammate's Nexconn registration failed."""
        with self.tx() as c:
            c.execute("DELETE FROM members WHERE user_id=? AND role='teammate'", (user_id,))

    def member_count(self, sandbox_id: str) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM members WHERE sandbox_id=?", (sandbox_id,)).fetchone()[0]

    def touch(self, sandbox_id: str, min_interval: float = 60.0) -> None:
        """Mark the sandbox as used now (written at most once a minute per sandbox)."""
        now = self.clock()
        with self.tx() as c:
            c.execute("UPDATE sandboxes SET last_seen=? WHERE id=? AND status='ACTIVE' AND last_seen < ?",
                      (now, sandbox_id, now - min_interval))

    def mark_room_joined(self, sandbox_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sandboxes SET room_joined=? WHERE id=? AND room_joined IS NULL",
                      (self.clock(), sandbox_id))

    def mark_group_ready(self, sandbox_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sandboxes SET group_ready=1 WHERE id=?", (sandbox_id,))

    def idle(self, cutoff: float) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute(
                "SELECT id FROM sandboxes WHERE status='ACTIVE' AND last_seen < ?", (cutoff,)).fetchall()]

    def active_ids(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute("SELECT id FROM sandboxes WHERE status='ACTIVE'").fetchall()]

    def expired_ids(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute("SELECT id FROM sandboxes WHERE status='EXPIRED'").fetchall()]

    def mark_expired(self, sandbox_id: str, idle_before: float | None = None) -> bool:
        """ACTIVE -> EXPIRED. With `idle_before`, only if the sandbox is STILL idle (its current
        last_seen is older than the cutoff), so a check made on a stale row cannot expire a sandbox
        another request has just used."""
        with self.tx() as c:
            if idle_before is None:
                return c.execute("UPDATE sandboxes SET status='EXPIRED' WHERE id=? AND status='ACTIVE'",
                                 (sandbox_id,)).rowcount == 1
            return c.execute("UPDATE sandboxes SET status='EXPIRED' WHERE id=? AND status='ACTIVE' AND last_seen < ?",
                             (sandbox_id, idle_before)).rowcount == 1

    def purge(self, sandbox_id: str) -> None:
        """Delete an EXPIRED sandbox's rows (its member names); the counters in meta stay."""
        with self.tx() as c:
            if c.execute("DELETE FROM sandboxes WHERE id=? AND status='EXPIRED'", (sandbox_id,)).rowcount:
                c.execute("DELETE FROM members WHERE sandbox_id=?", (sandbox_id,))

    def stats(self) -> dict[str, int]:
        with self.lock:
            rows = dict(self.conn.execute("SELECT status, COUNT(*) FROM sandboxes GROUP BY status").fetchall())
            members = self.conn.execute("SELECT COUNT(*) FROM members m JOIN sandboxes s ON s.id=m.sandbox_id "
                                        "WHERE s.status='ACTIVE'").fetchone()[0]
            mock = self.conn.execute("SELECT COUNT(*) FROM sandboxes WHERE status='ACTIVE' AND chat_mode='mock'"
                                     ).fetchone()[0]
        return {"active": rows.get("ACTIVE", 0), "expired": rows.get("EXPIRED", 0), "active_members": members,
                "active_offline_chat": mock, "live_users_registered": self.counter("live_users_registered"),
                "sandboxes_created": self.counter("sandboxes_created")}
