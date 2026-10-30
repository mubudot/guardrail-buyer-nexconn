"""Business layer: the BuyerService (catalog in catalog.py, SQLite store and order statuses in
store.py, audit/spend views in reporting.py, owner policy edits in policy_admin.py).

Every money-moving path goes through `BuyerService`, which re-runs the
guardrail engine server-side. The AI agent can *ask*; it only ever calls these
methods via tools. Safety properties enforced here (see tests/test_concurrency.py):

* budget check + provisional reservation happen in ONE SQLite write transaction
  (BEGIN IMMEDIATE) *before* PayPal is called, so concurrent requests (threads or
  processes sharing the DB file) cannot all pass against the same spend;
* order lifecycle changes are conditional UPDATEs (compare-and-set on status),
  with a durable CAPTURING state that cancel/expiry cannot override;
* an approval is claimed once (PENDING -> EXECUTING) together with its order and
  reservation; retries return the existing order;
* an approval is bound to its quote (product, quantity, amount, currency,
  merchant, category); a changed quote needs a fresh approval;
* ambiguous PayPal outcomes keep the reservation (NEEDS_RECONCILIATION) and are
  resolved from the provider's order status; only a confirmed COMPLETED capture
  counts as COMPLETED;
* recovery is monotonic and exclusive (fix round 5): every status change bumps a
  persisted per-order generation (orders.op_gen). Reconcile/capture results are
  applied only if the generation they started from is unchanged (a superseded read
  is rejected, result=SUPERSEDED); the only result accepted regardless is a confirmed
  COMPLETED capture, which is final. Once a capture outcome was ambiguous
  (orders.capture_unknown=1) the order never returns to PAYER_ACTION_REQUIRED and can
  never be cancelled or expired, only resolved from the provider.
* the schema is versioned (PRAGMA user_version, fix round 7): each migration runs in one
  BEGIN IMMEDIATE transaction with its backfill and version bump, so an interrupted
  upgrade is redone; DBs migrated by rounds 5/6 get a corrective capture_unknown backfill.
* mock-mode ownership is keyed on the canonical DB file (realpath + dev/inode), so path
  aliases of one database cannot both own it.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import json
import os
import secrets
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .guardrails import (Decision, OrderRequest, Policy, SpendLedger, Verdict,
                         evaluate, explain_rules, hard_rules_only, money)
from .catalog import Catalog
from .paypal_client import MockPayPal, PayPalClient, PayPalError
from .policy_admin import PolicyAdminMixin, PolicyConflict
from .reporting import ReportingMixin
from .store import (COUNTED_STATUSES, OPEN_STATUSES, RELEASED_STATUSES, RESERVED_STATUSES,  # noqa: F401
                    STUCK_AFTER, Store)

__all__ = ["BuyerService", "Catalog", "Store", "MockLockError", "PolicyConflict"]

log = logging.getLogger("guardrail")


class MockLockError(RuntimeError):
    """Another process already serves this database in mock mode."""


class BuyerService(ReportingMixin, PolicyAdminMixin):
    """Orders, approvals and payments (this file); audit/spend views in reporting.py;
    owner policy edits in policy_admin.py."""

    def __init__(self, policy: Policy, catalog: Catalog, paypal: PayPalClient,
                 db_path: str = ":memory:", clock=None, approval_ttl: timedelta | None = None):
        self.policy, self.catalog, self.paypal = policy, catalog, paypal
        self.store = Store(db_path)
        # same connection + lock: budget check and reservation share one transaction
        self.ledger = SpendLedger(conn=self.store.conn, lock=self.store.lock)
        self._clock = clock or (lambda: datetime.now(ZoneInfo("UTC")))
        self.approval_ttl = approval_ttl or timedelta(
            minutes=int(os.environ.get("PAYPAL_APPROVAL_TTL_MIN", "180")))
        self.base_policy = policy       # from policy.yaml; an owner edit (PUT /policy) overrides it
        self._policy_raw: str | None = None
        # Approval observers (e.g. the Nexconn chat approval channel). Called after the
        # transaction commits; an observer can never change or break a purchase.
        self._approval_listeners: list = []
        self.sync_policy()

    def add_approval_listener(self, fn) -> None:
        """fn(event, approval_row, result) with event in {"requested", "decided"}."""
        self._approval_listeners.append(fn)

    def _emit_approval(self, event: str, approval_id: str, result: dict[str, Any]) -> None:
        if not self._approval_listeners:
            return
        try:
            row = self.store.get("approvals", approval_id)
        except Exception:  # best effort: the decision itself is already committed
            log.exception("approval listener snapshot failed for %s (%s)", approval_id, event)
            return
        for fn in list(self._approval_listeners):
            try:
                fn(event, row, result)
            except Exception:  # an observer must never break the purchase flow
                log.exception("approval listener failed for %s (%s)", approval_id, event)

    def _ts(self, dt: datetime | None = None) -> str:
        return (dt or self._clock()).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

    # ---- helpers -------------------------------------------------------
    def _today(self) -> str:
        return self.policy.today(self._clock())

    def spent_today(self) -> Decimal:
        return self.ledger.spent_on(self._today(), self.policy.currency)

    def _line(self, product_id: str, quantity: int) -> tuple[dict[str, Any], OrderRequest]:
        p = self.catalog.get(product_id)
        if p is None:
            raise KeyError(f"unknown product_id {product_id}")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1 or quantity > 100:
            raise ValueError("quantity must be an integer between 1 and 100")
        amount = money(money(p["price"]) * quantity)
        req = OrderRequest(merchant=p["merchant"], category=p["category"], amount=amount,
                           currency=p.get("currency", "USD"), description=f"{quantity} x {p['name']}")
        return p, req

    @staticmethod
    def _quote(product_id: str, quantity: int, amount: Any, currency: str, merchant: str,
               category: str) -> dict[str, Any]:
        """The material terms a human approval is bound to."""
        return {"product_id": product_id, "quantity": int(quantity), "amount": str(money(amount)),
                "currency": str(currency).upper(), "merchant": merchant, "category": category}

    def _related_prior(self, request_id: str | None, product_id: str, merchant: str) -> Decimal:
        """Anti-splitting: amount already ordered (not released) for the same SKU at the
        same merchant within the same agent request."""
        if not request_id:
            return Decimal("0")
        qs = ",".join("?" for _ in COUNTED_STATUSES)
        with self.store.lock:
            rows = self.store.conn.execute(
                f"SELECT amount FROM orders WHERE request_id=? AND product_id=? AND lower(merchant)=lower(?) "
                f"AND status IN ({qs})", [request_id, product_id, merchant, *COUNTED_STATUSES]).fetchall()
        return money(sum((money(r[0]) for r in rows), Decimal("0")))

    def _group(self, request_id: str | None) -> str | None:
        """External agents' orders are grouped per policy day (`ext-<day>`, see GuardedTools).
        Re-resolve the day at decision time, inside the caller's transaction, so an approval
        requested before midnight and executed after it (or a call that crosses midnight) is
        aggregated with the orders of the day its reservation is booked on."""
        if isinstance(request_id, str) and request_id.startswith("ext-"):
            return f"ext-{self._today()}"
        return request_id

    def _evaluate(self, p: dict[str, Any], req: OrderRequest, request_id: str | None) -> Decision:
        return evaluate(self.policy, req, self.spent_today(),
                        self._related_prior(request_id, p["id"], p["merchant"]))

    # ---- read-only check ----------------------------------------------
    def check(self, product_id: str, quantity: int = 1, request_id: str | None = None) -> dict[str, Any]:
        self.sync_policy()
        self.expire_stale()  # unpaid orders past their TTL no longer count against the caps
        request_id = self._group(request_id)
        p, req = self._line(product_id, quantity)
        spent, prior = self.spent_today(), self._related_prior(request_id, p["id"], p["merchant"])
        d = evaluate(self.policy, req, spent, prior)
        self._log_decision("check", p, quantity, req, d, d.verdict.value,
                           explain_rules(self.policy, req, spent, prior), spent, request_id=request_id)
        return {"product_id": product_id, "product_name": p["name"], "quantity": quantity,
                "amount": str(req.amount), "currency": req.currency,
                "spent_today": str(self.spent_today()), **d.to_dict()}

    # ---- atomic state changes -----------------------------------------
    def _transition(self, oid: str, from_statuses: tuple[str, ...], to: str, release: bool = False,
                    expect_gen: int | None = None, extra_where: str = "", **fields: Any) -> int | None:
        """Compare-and-set the order status and bump its operation generation.
        Releases the reservation in the same transaction when `release`. With
        `expect_gen` the update applies only if no other operation changed the order
        since that generation was read. Returns the new generation (>= 1), or None (and
        changes nothing) if the order is no longer in one of `from_statuses` / the
        generation moved on."""
        sets = {"status": to, "updated_at": self._ts(), **fields}
        qs = ",".join("?" for _ in from_statuses)
        where, args = f"id=? AND status IN ({qs})", [oid, *from_statuses]
        if expect_gen is not None:
            where, args = where + " AND op_gen=?", [*args, expect_gen]
        if extra_where:
            where += f" AND ({extra_where})"
        with self.store.tx() as c:
            prev = c.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
            cur = c.execute(f"UPDATE orders SET {','.join(f'{k}=?' for k in sets)}, op_gen=op_gen+1 "
                            f"WHERE {where}", [*sets.values(), *args])
            if cur.rowcount != 1:
                return None
            if release:
                c.execute("DELETE FROM spend WHERE order_ref=?", (oid,))
            g = int(c.execute("SELECT op_gen FROM orders WHERE id=?", (oid,)).fetchone()[0])
            self._event(c, oid, prev[0] if prev else None, to, g,
                        fields.get("paypal_status") or ("reservation released" if release else None))
            return g

    def _event(self, c, oid: str, frm: str | None, to: str, gen: int, note: str | None = None) -> None:
        """Append one order status transition to the payment timeline (same transaction)."""
        c.execute("INSERT INTO order_events (order_id, at, from_status, to_status, op_gen, note) "
                  "VALUES (?,?,?,?,?,?)", (oid, self._ts(), frm, to, gen, (note or "")[:200] or None))

    def _log_decision(self, stage: str, p: dict[str, Any], quantity: int, req: OrderRequest,
                      d: Decision, outcome: str, rules: list[dict[str, Any]], spent_before: Any,
                      request_id: str | None = None, order_id: str | None = None,
                      approval_id: str | None = None) -> str:
        """Persist one guardrail decision (audit log). Joins the caller's transaction if any."""
        did = "DEC-" + uuid.uuid4().hex[:10].upper()
        hit = [r["rule"] for r in rules if r["outcome"] in ("DENY", "NEEDS_APPROVAL")]
        with self.store.tx():
            self.store.insert("decisions", {
                "id": did, "created_at": self._ts(), "stage": stage, "request_id": request_id,
                "product_id": p["id"], "product_name": p["name"], "merchant": p["merchant"],
                "category": p["category"], "quantity": int(quantity), "amount": str(req.amount),
                "currency": req.currency, "verdict": d.verdict.value, "outcome": outcome,
                "rule_hit": ", ".join(hit) or ("human_approval" if stage == "approval_execute"
                                               and d.verdict == Verdict.ALLOW else "none"),
                "reasons": json.dumps(list(d.reasons)), "rules": json.dumps(rules),
                "spent_before": str(money(spent_before)), "order_id": order_id,
                "approval_id": approval_id})
        return did

    def _superseded(self, oid: str, note: str = "") -> dict[str, Any]:
        """A result that arrived for an older generation of the order: not applied."""
        return {**self.store.get("orders", oid), "result": "SUPERSEDED",
                "note": note or "a newer operation changed this order first; this result was not applied"}

    def _reserve(self, p: dict[str, Any], req: OrderRequest, quantity: int, decision: Decision,
                 approval_id: str | None = None, request_id: str | None = None) -> str:
        """Insert the order (CREATING) and its spend reservation. Caller holds store.tx()."""
        oid = "ORD-" + uuid.uuid4().hex[:10].upper()
        now = self._ts()
        self.store.insert("orders", {
            "id": oid, "product_id": p["id"], "product_name": p["name"], "merchant": p["merchant"],
            "category": p["category"], "quantity": quantity, "amount": str(req.amount),
            "currency": req.currency, "status": "CREATING", "paypal_order_id": None,
            "paypal_status": None, "capture_id": None, "approval_id": approval_id,
            "decision": json.dumps(decision.to_dict()), "created_at": now,
            "approve_url": None, "updated_at": now, "request_id": request_id,
            "op_gen": 1, "capture_unknown": 0})
        self.ledger.record(self._today(), req.amount, req.currency, oid)
        self._event(self.store.conn, oid, None, "CREATING", 1, f"reserved {req.amount} {req.currency}")
        return oid

    # ---- signed cancel state (B3) ----------------------------------------
    def _state_secret(self) -> bytes:
        env = os.environ.get("STATE_SECRET")
        if env:
            return env.encode()
        if getattr(self, "_secret", None) is None:
            with self.store.tx() as c:  # one secret per database, shared by all processes
                c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('state_secret', ?)",
                          (secrets.token_hex(32),))
                self._secret = c.execute("SELECT value FROM meta WHERE key='state_secret'").fetchone()[0].encode()
        return self._secret

    def cancel_state(self, oid: str) -> str:
        """HMAC over our order id, put into the PayPal cancel_url when the order is created.
        GET /paypal/cancel must present it; the public PayPal order id alone is not enough."""
        return hmac.new(self._state_secret(), f"cancel:{oid}".encode(), hashlib.sha256).hexdigest()[:32]

    def verify_cancel_state(self, oid: str, state: str | None) -> bool:
        s = str(state or "")
        if not re.fullmatch(r"[0-9a-f]{32}", s):  # also keeps compare_digest on ASCII input
            return False
        return hmac.compare_digest(self.cancel_state(oid), s)

    # ---- money-moving --------------------------------------------------
    # Provider order statuses that mean "created, the buyer has not approved yet".
    AWAITING_BUYER = ("CREATED", "SAVED", "PAYER_ACTION_REQUIRED")

    # On a create *replay* (the first attempt's outcome was unknown) only these HTTP
    # statuses say the request itself is invalid, so the original attempt failed as well.
    # Anything else (401/403 auth, 404, 409, 429, ...) says nothing about the original
    # attempt: the order stays NEEDS_RECONCILIATION and keeps its reservation. A failed
    # OAuth token request (no Orders call made at all) never counts as evidence either.
    REPLAY_DEFINITIVE_STATUS = (400, 422)

    def _create_at_provider(self, oid: str, gen: int | None = None, replay: bool = False) -> dict[str, Any]:
        """Call PayPal create_order for a reserved CREATING order owned at generation
        `gen`. Idempotent: the PayPal request id is our order id, so a replay returns the
        same PayPal order -- possibly already approved or COMPLETED (N2), which is
        verified as a capture instead of being exposed as cancellable."""
        o = self.store.get("orders", oid)
        if gen is None:
            gen = int(o["op_gen"])
        try:
            created = self.paypal.create_order(money(o["amount"]), o["currency"],
                                               f"{o['quantity']} x {o['product_name']}", oid,
                                               items=[{"sku": o["product_id"], "quantity": o["quantity"]}],
                                               cancel_query=f"state={self.cancel_state(oid)}")
        except PayPalError as e:
            if e.ambiguous:
                return self._uncertain(oid, ("CREATING",), f"create outcome unknown: {e}", gen)
            if replay and (getattr(e, "source", "orders") != "orders"
                           or e.status_code not in self.REPLAY_DEFINITIVE_STATUS):
                return self._uncertain(oid, ("CREATING",), f"create replay refused, original outcome still "
                                                         f"unknown: {e}", gen)
            if not self._transition(oid, ("CREATING",), "FAILED", release=True, expect_gen=gen,
                                    paypal_status=str(e)[:200]):
                return self._superseded(oid)
            return {**self.store.get("orders", oid), "error": str(e)}
        except Exception as e:  # unexpected: treat as unknown outcome, keep the reservation
            return self._uncertain(oid, ("CREATING",), f"create outcome unknown: {type(e).__name__}: {e}", gen)
        st = created.get("status")
        ids = {"paypal_order_id": created["id"], "approve_url": created.get("approve_url")}
        if st in self.AWAITING_BUYER or st == "APPROVED":
            g = self._transition(oid, ("CREATING",), "PAYER_ACTION_REQUIRED", expect_gen=gen,
                                 paypal_status=st, **ids)
            if g is None:
                return self._superseded(oid)
            if st == "APPROVED":  # mock auto-approve, or a replay after the buyer approved
                return self._capture(oid, ("PAYER_ACTION_REQUIRED",), expect_gen=g)
            return self.store.get("orders", oid)
        # COMPLETED (or anything unexpected) on a create/replay: money may already have
        # moved. Never expose it as PAYER_ACTION_REQUIRED; verify with the provider.
        g = self._transition(oid, ("CREATING",), "NEEDS_RECONCILIATION", expect_gen=gen, capture_unknown=1,
                             paypal_status=f"create returned {st}; verifying capture"[:200], **ids)
        if g is None:
            return self._superseded(oid)
        return self._reconcile_once(oid, ("NEEDS_RECONCILIATION",), allow_capture=False, gen=g)

    def _uncertain(self, oid: str, from_statuses: tuple[str, ...], msg: str,
                   gen: int | None = None) -> dict[str, Any]:
        if not self._transition(oid, from_statuses, "NEEDS_RECONCILIATION", expect_gen=gen,
                                paypal_status=msg[:200]):
            return {**self._superseded(oid), "error": msg}
        return {**self.store.get("orders", oid), "error": msg}

    # PayPal issues that mean "the buyer has not approved yet": not fatal, keep the order open.
    NOT_YET_APPROVED = ("ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED")

    def _capture(self, oid: str, from_statuses: tuple[str, ...] = ("PAYER_ACTION_REQUIRED",),
                 expect_gen: int | None = None) -> dict[str, Any]:
        g = self._transition(oid, from_statuses, "CAPTURING", expect_gen=expect_gen)
        if g is None:
            o = self.store.get("orders", oid)
            if o["status"] == "COMPLETED":
                return {**o, "result": "ALREADY_CAPTURED"}
            if o["status"] == "CAPTURING":
                return {**o, "result": "CAPTURE_IN_PROGRESS"}
            if expect_gen is not None and o["status"] in from_statuses:
                return self._superseded(oid)
            raise ValueError(f"order {oid} is {o['status']}, cannot capture")
        o = self.store.get("orders", oid)
        unknown_before = bool(o.get("capture_unknown"))
        try:
            cap = self.paypal.capture_order(o["paypal_order_id"])
        except PayPalError as e:
            issue = getattr(e, "issue", None) or ""
            if issue in self.NOT_YET_APPROVED or any(t in str(e) for t in self.NOT_YET_APPROVED):
                # an earlier ambiguous capture keeps the order out of the cancellable state
                to = "NEEDS_RECONCILIATION" if unknown_before else "PAYER_ACTION_REQUIRED"
                if not self._transition(oid, ("CAPTURING",), to, expect_gen=g):
                    return self._superseded(oid)
                return {**self.store.get("orders", oid), "result": "NOT_APPROVED_YET", "error": str(e),
                        "paypal_issue": issue or None}
            if e.ambiguous or unknown_before or issue == "ORDER_ALREADY_CAPTURED":
                return self._after_uncertain_capture(oid, str(e), issue, g)
            if not self._transition(oid, ("CAPTURING",), "CAPTURE_FAILED", release=True, expect_gen=g,
                                    paypal_status=str(e)[:200]):
                return self._superseded(oid)
            return {**self.store.get("orders", oid), "error": str(e), "paypal_issue": issue or None}
        except Exception as e:  # anything else: the capture may have happened
            return self._after_uncertain_capture(oid, f"{type(e).__name__}: {e}", None, g)
        return self._apply_capture(oid, cap, ("CAPTURING",), expect_gen=g)

    def _after_uncertain_capture(self, oid: str, msg: str, issue: str | None, gen: int) -> dict[str, Any]:
        """Keep the reservation, mark the capture outcome unknown, then ask PayPal once."""
        g = self._transition(oid, ("CAPTURING",), "NEEDS_RECONCILIATION", expect_gen=gen, capture_unknown=1,
                             paypal_status=f"capture outcome unknown: {msg}"[:200])
        if g is None:
            return {**self._superseded(oid), "error": msg, "paypal_issue": issue or None}
        out = self._reconcile_once(oid, ("NEEDS_RECONCILIATION",), allow_capture=False, gen=g)
        return {**out, "error": msg, "paypal_issue": issue or None}

    def _apply_capture(self, oid: str, cap: dict[str, Any], from_statuses: tuple[str, ...],
                       expect_gen: int | None) -> dict[str, Any]:
        """Only a COMPLETED order with a COMPLETED capture counts as COMPLETED. That final
        state is accepted from CAPTURING / NEEDS_RECONCILIATION whatever the generation
        (it can only move an order forward); every other outcome needs `expect_gen`."""
        st, cap_status = cap.get("status"), cap.get("capture_status")
        paypal_status = f"{st}" + (f"/{cap_status}" if cap_status and cap_status != st else "")
        if st == "COMPLETED" and cap_status == "COMPLETED":
            ok = self._transition(oid, tuple(dict.fromkeys(from_statuses + ("CAPTURING", "NEEDS_RECONCILIATION"))),
                                  "COMPLETED", paypal_status=paypal_status, capture_id=cap.get("capture_id"))
        elif cap_status in ("DECLINED", "FAILED") or st == "VOIDED":
            ok = self._transition(oid, from_statuses, "CAPTURE_FAILED", release=True, expect_gen=expect_gen,
                                  paypal_status=paypal_status, capture_id=cap.get("capture_id"))
        else:  # capture PENDING, or no capture status: money may move later
            ok = self._transition(oid, from_statuses, "NEEDS_RECONCILIATION", expect_gen=expect_gen,
                                  capture_unknown=1, capture_id=cap.get("capture_id"),
                                  paypal_status=f"{st}/{cap_status or 'NO_CAPTURE_STATUS'}")
        if not ok:
            cur = self.store.get("orders", oid)
            if cur["status"] == "COMPLETED":
                return cur
            return self._superseded(oid)
        return self.store.get("orders", oid)

    # ---- reconciliation -------------------------------------------------
    def _reconcile_once(self, oid: str, from_statuses: tuple[str, ...], allow_capture: bool,
                        gen: int | None = None) -> dict[str, Any]:
        """One provider read applied against the generation it started from."""
        o = self.store.get("orders", oid)
        if gen is None:
            gen = int(o["op_gen"])
        if o["status"] not in from_statuses or int(o["op_gen"]) != gen:
            return self._superseded(oid)
        if not o.get("paypal_order_id"):
            # create outcome unknown: replay create with the same idempotency key
            g = self._transition(oid, from_statuses, "CREATING", expect_gen=gen)
            if g is None:
                return self._superseded(oid)
            out = self._create_at_provider(oid, g, replay=True)
            return {**out, "result": out.get("result") or self._result(out)}
        try:
            live = self.paypal.get_order(o["paypal_order_id"])
        except (PayPalError, NotImplementedError) as e:
            return {**o, "result": "STILL_UNCONFIRMED", "error": str(e)}
        st = live.get("status")
        unknown = bool(o.get("capture_unknown"))
        ok = True
        if st == "COMPLETED":
            out = self._apply_capture(oid, live, from_statuses, expect_gen=gen)
        elif st == "APPROVED" and allow_capture:
            out = self._capture(oid, from_statuses, expect_gen=gen)
        elif st in self.AWAITING_BUYER and not unknown:
            ok = self._transition(oid, from_statuses, "PAYER_ACTION_REQUIRED", expect_gen=gen, paypal_status=st)
            out = self.store.get("orders", oid)
        elif st == "VOIDED":
            ok = self._transition(oid, from_statuses, "CANCELLED", release=True, expect_gen=gen, paypal_status=st)
            out = self.store.get("orders", oid)
        else:
            # APPROVED without permission to capture, or awaiting the buyer after an
            # ambiguous capture: stay reserved and non-cancellable (never move backward).
            cur = self.store.get("orders", oid)
            if int(cur["op_gen"]) != gen:
                return {**self._superseded(oid), "paypal_live_status": st}
            out = {**cur, "paypal_live_status": st}
        if not ok:
            return self._superseded(oid)
        out = dict(out)
        out.setdefault("result", self._result(out))
        return out

    def reconcile(self, oid: str) -> dict[str, Any]:
        """Resolve an order whose payment outcome is unknown from PayPal's order status.
        Also picks up CREATING/CAPTURING orders stuck for STUCK_AFTER (e.g. a crash)."""
        o = self.store.get("orders", oid)
        if o is None:
            raise KeyError(oid)
        stuck_cutoff = self._ts(self._clock() - STUCK_AFTER)
        gen = int(o["op_gen"])
        if o["status"] == "NEEDS_RECONCILIATION":
            return self._reconcile_once(oid, ("NEEDS_RECONCILIATION",), allow_capture=True, gen=gen)
        if o["status"] in ("CREATING", "CAPTURING") and (o.get("updated_at") or "") < stuck_cutoff:
            if o["status"] == "CAPTURING":
                # claim the stuck capture first: a capture call was made, its outcome is unknown
                g = self._transition(oid, ("CAPTURING",), "NEEDS_RECONCILIATION", expect_gen=gen,
                                     capture_unknown=1, paypal_status="capture stuck; outcome unknown")
                if g is None:
                    return self._superseded(oid)
                return self._reconcile_once(oid, ("NEEDS_RECONCILIATION",), allow_capture=True, gen=g)
            return self._reconcile_once(oid, ("CREATING",), allow_capture=True, gen=gen)
        return {**o, "result": "NOTHING_TO_RECONCILE"}

    # ---- order status / manual capture / cancel by our order id -------
    def get_order(self, oid: str, refresh: bool = False) -> dict[str, Any]:
        """Our order record; with refresh=True also fetch the live status from PayPal."""
        self.expire_stale()
        o = self.store.get("orders", oid)
        if o is None:
            raise KeyError(oid)
        if refresh and o.get("paypal_order_id"):
            try:
                live = self.paypal.get_order(o["paypal_order_id"])
            except (PayPalError, NotImplementedError) as e:
                o["refresh_error"] = str(e)
                return o
            o["paypal_live_status"] = live["status"]
        return o

    def capture(self, oid: str) -> dict[str, Any]:
        """Try to capture an open order (e.g. buyer approved but the redirect was lost).
        Returns result=NOT_APPROVED_YET without changing anything if the buyer has not approved."""
        self.expire_stale()
        o = self.store.get("orders", oid)
        if o is None:
            raise KeyError(oid)
        if o["status"] == "COMPLETED":
            return {**o, "result": "ALREADY_CAPTURED"}
        if o["status"] == "CAPTURING":
            return {**o, "result": "CAPTURE_IN_PROGRESS"}
        if o["status"] == "NEEDS_RECONCILIATION":
            return self.reconcile(oid)
        if o["status"] != "PAYER_ACTION_REQUIRED":
            raise ValueError(f"order {oid} is {o['status']}, cannot capture")
        out = self._capture(oid)
        out.setdefault("result", "CAPTURED" if out["status"] == "COMPLETED" else out["status"])
        return out

    def cancel(self, oid: str) -> dict[str, Any]:
        """Cancel an order that is still waiting for the buyer, and release its reservation,
        atomically. Orders being captured, captured, or of unknown outcome are never
        cancelled here (result=NOT_CANCELLABLE). PayPal Orders v2 has no cancel call for
        uncaptured orders; an unapproved PayPal order simply expires, and our status check
        refuses to capture a CANCELLED order."""
        if self.store.get("orders", oid) is None:
            raise KeyError(oid)
        if self._transition(oid, ("PAYER_ACTION_REQUIRED",), "CANCELLED", release=True,
                            extra_where="capture_unknown=0"):
            return {**self.store.get("orders", oid), "result": "CANCELLED"}
        o = self.store.get("orders", oid)
        return {**o, "result": "ALREADY_CANCELLED" if o["status"] == "CANCELLED" else "NOT_CANCELLABLE"}

    # ---- PayPal buyer-approval round trip --------------------------------
    def _by_paypal_id(self, paypal_order_id: str) -> dict[str, Any]:
        o = self.store.find("orders", paypal_order_id=paypal_order_id)
        if o is None:
            raise KeyError(paypal_order_id)
        return o

    def complete_payment(self, paypal_order_id: str) -> dict[str, Any]:
        """Buyer returned from PayPal after approving: capture (idempotent)."""
        return self.capture(self._by_paypal_id(paypal_order_id)["id"])

    def cancel_payment(self, paypal_order_id: str, state: str | None = None) -> dict[str, Any]:
        """Buyer cancelled on PayPal: release the reserved spend if still awaiting approval.
        `state` must be the HMAC put into this order's cancel_url (PermissionError otherwise)."""
        o = self._by_paypal_id(paypal_order_id)
        if not self.verify_cancel_state(o["id"], state):
            raise PermissionError("missing or invalid signed cancel state")
        return self.cancel(o["id"])

    def expire_stale(self) -> list[str]:
        """Expire orders the buyer never approved within approval_ttl (atomically; an
        order already moved to CAPTURING is never expired)."""
        cutoff = self._ts(self._clock() - self.approval_ttl)
        q = ("SELECT id FROM orders WHERE status='PAYER_ACTION_REQUIRED' AND capture_unknown=0 "
             "AND created_at < ?")
        with self.store.lock:
            if not self.store.conn.execute(q, (cutoff,)).fetchone():
                return []
        expired = []
        with self.store.tx() as c:
            for (oid,) in c.execute(q, (cutoff,)).fetchall():
                cur = c.execute("UPDATE orders SET status='EXPIRED', updated_at=?, op_gen=op_gen+1 "
                                "WHERE id=? AND status='PAYER_ACTION_REQUIRED' AND capture_unknown=0",
                                (self._ts(), oid))
                if cur.rowcount == 1:
                    c.execute("DELETE FROM spend WHERE order_ref=?", (oid,))
                    g = int(c.execute("SELECT op_gen FROM orders WHERE id=?", (oid,)).fetchone()[0])
                    self._event(c, oid, "PAYER_ACTION_REQUIRED", "EXPIRED", g,
                                "buyer did not approve in time; reservation released")
                    expired.append(oid)
        return expired

    def db_identity(self) -> tuple[str, int, int]:
        """Canonical identity of the database file (CODEX_REVIEW_4 N2): the fully
        resolved path (symlinks, `..`, relative paths) plus its device/inode, so every
        alias of one file maps to the same owner."""
        real = os.path.realpath(self.store.db_path)
        st = os.stat(real)
        return real, st.st_dev, st.st_ino

    def acquire_mock_lock(self) -> None:
        """Mock mode is single-process (N3): MockPayPal state lives in this process's
        memory, so two processes on one database would each see the other's orders as
        "lost". Take an exclusive, non-blocking lock file next to the DB for the lifetime
        of this service; fail fast if another process holds it.

        Fix round 7 (CODEX_REVIEW_4 N2): ownership is keyed on the canonical DB identity,
        not on the path string. The lock file sits next to the *resolved* DB file, so a
        symlink / relative / `..` alias of the same file contends for the same lock. A
        hard link has no single canonical path, so a DB file with more than one link is
        refused in mock mode (fail closed). After locking, the DB's device/inode are
        re-checked so the file cannot be swapped between resolving and locking."""
        if not isinstance(self.paypal, MockPayPal) or self.store.db_path in (":memory:", ""):
            return
        if getattr(self, "_mock_lock", None) is not None:
            return
        real, dev, ino = self.db_identity()
        if os.stat(real).st_nlink > 1:
            raise MockLockError(
                f"PAYPAL_MODE=mock: database {real} has {os.stat(real).st_nlink} hard links; mock "
                f"ownership cannot be verified across hard links. Use a single path (or Sandbox mode).")
        path = f"{real}.mock.lock"
        fh = open(path, "a+")
        try:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:  # pragma: no cover - Windows
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            alias = "" if real == self.store.db_path else f" (resolved from {self.store.db_path})"
            raise MockLockError(
                f"PAYPAL_MODE=mock is single-process: another GuardRail Buyer process already uses "
                f"{real}{alias} (lock file {path}). Stop it, or use a different DB_PATH. "
                f"Do not run mock mode with several workers.") from None
        if self.db_identity() != (real, dev, ino):  # pragma: no cover - race: file replaced
            fh.close()
            raise MockLockError(f"database {self.store.db_path} changed identity while locking; retry")
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} dev={dev} ino={ino} db={real}\n")
        fh.flush()
        self._mock_lock = fh

    def release_mock_lock(self) -> None:
        fh = getattr(self, "_mock_lock", None)
        if fh is not None:
            fh.close()  # closing the descriptor drops the flock
            self._mock_lock = None

    def invalidate_lost_mock_orders(self) -> list[str]:
        """MockPayPal keeps its orders in memory. After a restart, open orders that point
        at a mock order this process does not know can never be approved or captured:
        mark them MOCK_RESET and release their reservations (B9). Only runs once this
        process holds the exclusive mock lock (N3), so it can never touch the orders of
        another live worker."""
        if not isinstance(self.paypal, MockPayPal):
            return []
        self.acquire_mock_lock()
        lost = []
        for st in ("PAYER_ACTION_REQUIRED", "CAPTURING", "NEEDS_RECONCILIATION"):
            for o in self.store.list("orders", st):
                pid = o.get("paypal_order_id")
                if pid and pid.startswith("MOCK-") and not self.paypal.known(pid):
                    if self._transition(o["id"], (st,), "MOCK_RESET", release=True,
                                        paypal_status="mock order lost on restart"):
                        lost.append(o["id"])
        return lost

    @staticmethod
    def _result(order: dict[str, Any]) -> str:
        st = order["status"]
        if st == "PAYER_ACTION_REQUIRED":
            return "AWAITING_PAYER_APPROVAL"
        if st == "COMPLETED":
            return "ORDER_PLACED"
        if st in ("CREATING", "CAPTURING", "NEEDS_RECONCILIATION"):
            return "PAYMENT_UNCONFIRMED"
        return "FAILED"

    def place_order(self, product_id: str, quantity: int = 1, request_id: str | None = None) -> dict[str, Any]:
        p, req = self._line(product_id, quantity)
        self.expire_stale()  # release expired reservations first (own transaction; only ever loosens the caps)
        with self.store.tx():  # check + reserve atomically, before any PayPal I/O (B1)
            self.sync_policy()
            request_id = self._group(request_id)
            spent, prior = self.spent_today(), self._related_prior(request_id, p["id"], p["merchant"])
            d = evaluate(self.policy, req, spent, prior)
            rules = explain_rules(self.policy, req, spent, prior)
            if d.verdict == Verdict.DENY:
                self._log_decision("order", p, quantity, req, d, "DENIED", rules, spent, request_id)
                return {"result": "DENIED", **d.to_dict()}
            if d.verdict == Verdict.NEEDS_APPROVAL:
                self._log_decision("order", p, quantity, req, d, "NEEDS_APPROVAL", rules, spent, request_id)
                return {"result": "NEEDS_APPROVAL", **d.to_dict(),
                        "next_step": "call request_approval with a justification; a human must approve"}
            oid = self._reserve(p, req, quantity, d, request_id=request_id)
            self._log_decision("order", p, quantity, req, d, "RESERVED", rules, spent, request_id, order_id=oid)
        order = self._create_at_provider(oid)
        return {"result": self._result(order), "order": order, **d.to_dict()}

    def request_approval(self, product_id: str, quantity: int, justification: str,
                         request_id: str | None = None) -> dict[str, Any]:
        self.sync_policy()
        self.expire_stale()
        request_id = self._group(request_id)
        p, req = self._line(product_id, quantity)
        spent, prior = self.spent_today(), self._related_prior(request_id, p["id"], p["merchant"])
        d = evaluate(self.policy, req, spent, prior)
        rules = explain_rules(self.policy, req, spent, prior)
        if d.verdict == Verdict.DENY:
            self._log_decision("approval_request", p, quantity, req, d, "DENIED", rules, spent, request_id)
            return {"result": "DENIED", **d.to_dict(), "note": "denied orders cannot be escalated"}
        if d.verdict == Verdict.ALLOW:
            self._log_decision("approval_request", p, quantity, req, d, "NOT_NEEDED", rules, spent, request_id)
            return {"result": "NOT_NEEDED", **d.to_dict(), "note": "within policy; use create_order"}
        aid = "APR-" + uuid.uuid4().hex[:8].upper()
        with self.store.tx():
            self.store.insert("approvals", {
                "id": aid, "product_id": p["id"], "product_name": p["name"], "merchant": p["merchant"],
                "category": p["category"], "quantity": quantity, "amount": str(req.amount),
                "currency": req.currency, "justification": justification[:500],
                "reasons": json.dumps(list(d.reasons)), "status": "PENDING", "order_id": None,
                "created_at": self._ts(), "decided_at": None, "request_id": request_id})
            self._log_decision("approval_request", p, quantity, req, d, "APPROVAL_REQUESTED", rules, spent,
                               request_id, approval_id=aid)
        out = {"result": "APPROVAL_REQUESTED", "approval_id": aid, **d.to_dict()}
        self._emit_approval("requested", aid, out)
        return out

    def approve(self, approval_id: str) -> dict[str, Any]:
        """Human approval (see _approve); observers hear about a first decision, not a replay."""
        out = self._approve(approval_id)
        if not out.get("replayed"):
            self._emit_approval("decided", approval_id, out)
        return out

    def _approve(self, approval_id: str) -> dict[str, Any]:
        """Human approval. Claims the approval exactly once (PENDING -> EXECUTING) in the
        same transaction that creates its single order and reservation. A repeated call
        returns the existing order. A changed quote (B5) or a deny rule refuses."""
        self.expire_stale()
        with self.store.tx() as c:
            self.sync_policy()
            a = self.store.get("approvals", approval_id)
            if a is None:
                raise KeyError(approval_id)
            if a["status"] != "PENDING":
                if a.get("order_id"):
                    o = self.store.get("orders", a["order_id"])
                    return {"result": self._result(o), "order": o, "replayed": True}
                raise ValueError(f"approval is already {a['status']}")
            approved = self._quote(a["product_id"], a["quantity"], a["amount"], a["currency"],
                                   a["merchant"], a["category"])
            p = self.catalog.get(a["product_id"])
            current = None
            if p is not None:
                current = self._quote(p["id"], a["quantity"], money(money(p["price"]) * int(a["quantity"])),
                                      p.get("currency", "USD"), p["merchant"], p["category"])
            if current != approved:
                c.execute("UPDATE approvals SET status='QUOTE_CHANGED', decided_at=? WHERE id=? AND status='PENDING'",
                          (self._ts(), approval_id))
                return {"result": "QUOTE_CHANGED", "approval_id": approval_id, "approved_quote": approved,
                        "current_quote": current,
                        "note": "the product's terms changed since approval was requested; nothing was "
                                "bought. Request a fresh approval for the current quote."}
            p, req = self._line(a["product_id"], int(a["quantity"]))
            # Policy may have changed since the request, and other purchases for the same
            # SKU/merchant in the same agent request may have executed meanwhile (N4):
            # recheck the hard rules (incl. the aggregate hard maximum) inside this
            # transaction. Human approval waives only the soft caps.
            group = self._group(a.get("request_id"))
            prior = self._related_prior(group, p["id"], p["merchant"])
            d = hard_rules_only(self.policy, req, prior)
            spent = self.spent_today()
            rules = explain_rules(self.policy, req, spent, prior, soft_waived=True)
            if d.verdict == Verdict.DENY:
                c.execute("UPDATE approvals SET status='DENIED_BY_POLICY', decided_at=? WHERE id=? AND status='PENDING'",
                          (self._ts(), approval_id))
                self._log_decision("approval_execute", p, int(a["quantity"]), req, d, "DENIED", rules, spent,
                                   group, approval_id=approval_id)
                return {"result": "DENIED", **d.to_dict()}
            claim = c.execute("UPDATE approvals SET status='EXECUTING', decided_at=? WHERE id=? AND status='PENDING'",
                              (self._ts(), approval_id))
            if claim.rowcount != 1:  # pragma: no cover - impossible inside the write transaction
                raise ValueError("approval was claimed concurrently")
            oid = self._reserve(p, req, int(a["quantity"]), d, approval_id=approval_id,
                                request_id=group)
            c.execute("UPDATE approvals SET order_id=? WHERE id=?", (oid, approval_id))
            self._log_decision("approval_execute", p, int(a["quantity"]), req, d, "RESERVED", rules, spent,
                               group, order_id=oid, approval_id=approval_id)
        order = self._create_at_provider(oid)
        with self.store.tx() as c:
            c.execute("UPDATE approvals SET status='APPROVED' WHERE id=? AND status='EXECUTING'", (approval_id,))
        return {"result": self._result(order), "order": order}

    def reject(self, approval_id: str) -> dict[str, Any]:
        out = self._reject(approval_id)
        self._emit_approval("decided", approval_id, out)
        return out

    def _reject(self, approval_id: str) -> dict[str, Any]:
        with self.store.tx() as c:
            a = self.store.get("approvals", approval_id)
            if a is None:
                raise KeyError(approval_id)
            cur = c.execute("UPDATE approvals SET status='REJECTED', decided_at=? WHERE id=? AND status='PENDING'",
                            (self._ts(), approval_id))
            if cur.rowcount != 1:
                raise ValueError(f"approval is already {a['status']}")
        return {"result": "REJECTED", "approval_id": approval_id}
