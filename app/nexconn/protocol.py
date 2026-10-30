"""What the agent posts into the approval room, and how a human's reply is read.

Messages are plain Nexconn text messages (RC:TxtMsg), so they read well in any Nexconn
client. The machine-readable part rides in the message's `extra` field (a JSON string):

    request card (agent -> room):  {"grb": "approval_request", "approval_id": "APR-…", "v": 1}
    decision (approver -> room):   {"grb": "decision", "approval_id": "APR-…", "decision": "approve"}
    outcome (agent -> room):       {"grb": "outcome", "approval_id": "APR-…", "result": "…"}

A human can also just type `approve APR-1A2B3C4D` or `reject APR-1A2B3C4D` (any case); the
buttons on the /chat page send the same text plus the structured `extra`. A decision always
names the approval id, so a stray "ok" or "yes" never approves anything.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

APPROVAL_ID = re.compile(r"APR-[0-9A-F]{8}")
_COMMAND = re.compile(r"^\s*/?(approve|reject)\s+(APR-[0-9A-F]{8})\s*[.!]?\s*$", re.IGNORECASE)
_MAX_EXTRA = 2048


@dataclass(frozen=True)
class Decision:
    approval_id: str
    decision: str          # "approve" | "reject"


def _extra(content: Any) -> dict[str, Any]:
    extra = content.get("extra") if isinstance(content, dict) else None
    if not isinstance(extra, str) or not extra or len(extra) > _MAX_EXTRA:
        return {}
    try:
        data = json.loads(extra)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_content(raw: Any) -> dict[str, Any]:
    """RC:TxtMsg content arrives as a JSON string (server API / webhook) or as an object."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and len(raw) <= 12_000:
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def parse_decision(message_type: str, content: Any) -> Decision | None:
    """A decision from a text message, or None. Structured `extra` wins over the text; the
    two must agree when both are present (a mismatch is ignored rather than guessed)."""
    if message_type != "RC:TxtMsg":
        return None
    body = parse_content(content)
    text = body.get("content")
    by_text = None
    if isinstance(text, str) and len(text) <= 200:
        m = _COMMAND.match(text)
        if m:
            by_text = Decision(m.group(2).upper(), m.group(1).lower())
    x = _extra(body)
    by_extra = None
    if x.get("grb") == "decision":
        aid, dec = x.get("approval_id"), x.get("decision")
        if isinstance(aid, str) and APPROVAL_ID.fullmatch(aid) and dec in ("approve", "reject"):
            by_extra = Decision(aid, dec)
    if by_extra and by_text and by_extra != by_text:
        return None
    return by_extra or by_text


def _money(amount: Any, currency: Any) -> str:
    return f"{amount} {currency}"


def request_card(approval: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Text + extra for a new approval request."""
    aid = approval["id"]
    reasons = approval.get("reasons") or []
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except ValueError:
            reasons = [reasons]
    why = "; ".join(str(r) for r in reasons[:3]) or "over a soft cap"
    lines = [
        f"Approval needed · {aid}",
        f"{approval.get('quantity')} × {approval.get('product_name')} from {approval.get('merchant')}"
        f" = {_money(approval.get('amount'), approval.get('currency'))}",
        f"Guardrail: {why}",
        f"Agent's reason: {str(approval.get('justification') or '')[:300]}",
        f"Reply “approve {aid}” or “reject {aid}”.",
    ]
    extra = {"grb": "approval_request", "v": 1, "approval_id": aid,
             "amount": str(approval.get("amount")), "currency": approval.get("currency"),
             "product": approval.get("product_name"), "merchant": approval.get("merchant"),
             "quantity": approval.get("quantity")}
    return "\n".join(lines), extra


_OUTCOME_TEXT = {
    "AWAITING_PAYER_APPROVAL": "approved — PayPal order created; pay it with PayPal from the dashboard",
    "ORDER_PLACED": "approved — paid with PayPal",
    "PAYMENT_UNCONFIRMED": "approved — PayPal outcome not confirmed yet (budget stays reserved)",
    "FAILED": "approved, but the PayPal order failed (nothing was charged)",
    "DENIED": "not bought — a hard rule blocks it now (approval cannot waive a deny)",
    "QUOTE_CHANGED": "not bought — the price or terms changed; the agent must ask again",
    "REJECTED": "rejected — nothing was bought",
}


def outcome_text(approval_id: str, result: dict[str, Any], via: str, by: str | None) -> tuple[str, dict]:
    res = str(result.get("result") or "")
    what = _OUTCOME_TEXT.get(res, res.lower().replace("_", " ") or "decided")
    who = f" by {by}" if by else ""
    where = {"chat": "in chat", "dashboard": "on the dashboard"}.get(via, via)
    order = result.get("order") or {}
    order_id = order.get("id") if isinstance(order, dict) else None
    text = f"{approval_id}: {what} (decided{who} {where})"
    if order_id:
        text += f" · order {order_id}"
    extra = {"grb": "outcome", "approval_id": approval_id, "result": res, "via": via}
    if order_id:
        extra["order_id"] = order_id
    return text, extra


def refusal_text(approval_id: str, why: str) -> tuple[str, dict]:
    return f"Note · {approval_id}: {why}", {"grb": "notice", "approval_id": approval_id}
