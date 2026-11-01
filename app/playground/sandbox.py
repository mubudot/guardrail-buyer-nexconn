"""One playground sandbox: its own BuyerService (own SQLite file, own copy of the policy, simulated
PayPal with a checkout page), the rule-based OfflineAgent, and a chat approval channel bound to the
sandbox's own Nexconn group. Never touches the main database or the judges' room."""
from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path
from typing import Any

from ..catalog import Catalog
from ..guardrails import Policy
from ..nexconn.channel import ChatApprovalChannel
from ..nexconn.client import NexconnError
from ..nexconn.config import NexconnConfig
from ..offline_agent import OfflineAgent
from ..paypal_client import MockPayPal
from ..service import BuyerService

log = logging.getLogger("guardrail.playground")

_QTY_ID = re.compile(r"\b(\d+) x (P-\d{4})\b")
PAY_HINT = "pay it on the simulated checkout (Pay button in your sandbox guide)"
DECIDED = ("APPROVED", "REJECTED", "DENIED_BY_POLICY", "QUOTE_CHANGED", "EXECUTING")


class SandboxChannel(ChatApprovalChannel):
    """The approval channel for one sandbox room. Users are registered by the manager (with the
    visitor's display name), so bootstrap only makes sure the group exists with its members:
    live groups once (remembered in the registry), the in-process stand-in every time it starts."""

    pay_hint = PAY_HINT

    def __init__(self, svc, cfg: NexconnConfig, client, names: dict[str, str], group_ready: bool,
                 on_group_ready=None):
        super().__init__(svc, cfg, client, dispatch="inline")
        self.names = dict(names)
        self._group_ready = group_ready and cfg.mode == "live"
        self._on_group_ready = on_group_ready

    def bootstrap(self) -> bool:
        with self._boot_lock:
            if self.ready:
                return True
            try:
                if not self._group_ready:
                    self.client.ensure_group(self.cfg.group_id, self.cfg.group_name, self.cfg.bot_user_id,
                                             [self.cfg.bot_user_id, *self.cfg.approver_ids])
                    if self.cfg.mode == "live" and self._on_group_ready:
                        self._on_group_ready()
                    self._group_ready = self.cfg.mode == "live"
            except NexconnError as e:
                self.last_error = f"bootstrap: {e}"
                log.warning("sandbox room %s bootstrap failed: %s", self.cfg.group_id, e)
                return False
            self.ready, self.last_error = True, None
            return True

    def display_name(self, user_id: str | None) -> str | None:
        return self.names.get(user_id, user_id) if user_id else user_id

    def add_member(self, user_id: str, name: str) -> None:
        """A teammate joined: they become an approver of this room and are added to the group."""
        self.names[user_id] = name
        if user_id not in self.cfg.approver_ids:
            self.cfg = dataclasses.replace(self.cfg, approver_ids=(*self.cfg.approver_ids, user_id))
        self.client.ensure_group(self.cfg.group_id, self.cfg.group_name, self.cfg.bot_user_id,
                                 [self.cfg.bot_user_id, *self.cfg.approver_ids])

    def session(self, user_id: str | None) -> dict[str, Any]:
        if user_id not in self.cfg.approver_ids:
            raise PermissionError("not a member of this sandbox")
        self.bootstrap()
        token = self.client.issue_token(user_id, self.names.get(user_id, user_id))
        return {"mode": self.cfg.mode, "app_key": self.cfg.app_key if self.cfg.mode == "live" else "",
                "area": self.cfg.area, "token": token, "user_id": user_id, "group_id": self.cfg.group_id,
                "bot_user_id": self.cfg.bot_user_id, "approver_ids": list(self.cfg.approver_ids)}

    def post(self, kind: str, text: str, extra: dict[str, Any] | None = None) -> None:
        """A bot message that is not about one approval (welcome, reset notice)."""
        self._enqueue("SANDBOX", kind, text, {"grb": "notice", **(extra or {})})


class Sandbox:
    def __init__(self, row: dict[str, Any], members: list[dict[str, Any]], *, db_path: Path,
                 base_cfg: NexconnConfig, client, paypal: MockPayPal, policy: Policy, catalog: Catalog,
                 on_group_ready=None):
        self.id, self.group_id, self.name = row["id"], row["group_id"], row["name"]
        self.chat_mode = row["chat_mode"]
        self.paypal = paypal
        self.svc = BuyerService(policy, catalog, paypal, str(db_path))
        try:
            # mock orders this process no longer knows (restart) can never be paid: MOCK_RESET
            self.svc.invalidate_lost_mock_orders()
            cfg = dataclasses.replace(
                base_cfg, mode="live" if self.chat_mode == "live" else "mock", group_id=self.group_id,
                group_name=f"GuardRail sandbox · {self.name}"[:64],
                approver_ids=tuple(m["user_id"] for m in members))
            self.channel = SandboxChannel(self.svc, cfg, client, {m["user_id"]: m["name"] for m in members},
                                          group_ready=bool(row.get("group_ready")),
                                          on_group_ready=on_group_ready)
            self.agent = OfflineAgent(self.svc)
            if self.chat_mode == "mock" and row.get("fallback_reason") == "upstream":
                client.issue_token(cfg.bot_user_id, "GuardRail agent")
                for member in members:
                    client.issue_token(member["user_id"], member["name"])
                self.channel.bootstrap()
                if not client.history(self.group_id):
                    self.channel.restore_pending()
            self.channel.recover()          # re-send room posts a previous run left queued or failed
        except BaseException:
            self.close()
            raise
        self.last_retry = 0.0

    def close(self) -> None:
        try:
            self.svc.release_mock_lock()
        finally:
            with self.svc.store.lock:
                self.svc.store.conn.close()

    # ---- views -----------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        svc = self.svc
        svc.expire_stale()
        pol, _ = svc.policy_snapshot()
        orders = svc.store.list("orders")
        approvals = svc.store.list("approvals")
        with svc.store.lock:
            denied = svc.store.conn.execute(
                "SELECT 1 FROM decisions WHERE outcome='DENIED' LIMIT 1").fetchone() is not None
        steps = {
            "auto_bought": any(not o.get("approval_id") for o in orders),
            "approval_requested": bool(approvals),
            "decided": any(a["status"] in DECIDED for a in approvals),
            "paid": any(o["status"] == "COMPLETED" for o in orders),
            "denied": denied,
        }
        return {
            "limits": {"currency": pol["currency"], "per_order_cap": pol["per_order_cap"],
                       "daily_cap": pol["daily_cap"], "hard_max_per_order": pol["hard_max_per_order"],
                       "spent_today": str(svc.spent_today())},
            "approvals": [{k: a.get(k) for k in ("id", "product_name", "merchant", "quantity", "amount", "currency",
                                                  "status", "justification", "order_id", "created_at")}
                          for a in approvals[:50]],
            "orders": [{**{k: o.get(k) for k in ("id", "product_name", "quantity", "amount", "currency", "status",
                                                   "approval_id", "created_at")},
                        "pay_url": o.get("approve_url") if o["status"] == "PAYER_ACTION_REQUIRED" else None}
                       for o in orders[:50]],
            "steps": steps,
        }

    def readable(self, text: str) -> str:
        """The offline agent's summary names products by id ("2 x P-1001"): use their names."""
        def name(m: re.Match) -> str:
            p = self.svc.catalog.get(m.group(2))
            return f"{m.group(1)} × {p['name']}" if p else m.group(0)
        return _QTY_ID.sub(name, text)

    @staticmethod
    def pay_links(result: dict[str, Any]) -> list[dict[str, Any]]:
        """Checkout links for the orders an agent run left awaiting payment."""
        out = []
        for t in result.get("transcript") or []:
            o = (t.get("output") or {}).get("order") if isinstance(t.get("output"), dict) else None
            if isinstance(o, dict) and o.get("status") == "PAYER_ACTION_REQUIRED" and o.get("approve_url"):
                out.append({"order_id": o["id"], "url": o["approve_url"], "amount": o.get("amount"),
                            "currency": o.get("currency"), "product": o.get("product_name")})
        return out
