"""The mock PayPal checkout page (stand-in for PayPal's approval page), shared by the main app
(PAYPAL_MODE=mock + PAYPAL_MOCK_APPROVAL=1) and the /try playground (always simulated)."""
from __future__ import annotations

import html
from typing import Any


def mock_checkout_page(token: str, order: dict[str, Any], base: str = "", note: str = "") -> str:
    """HTML for one mock order. `base` prefixes the approve / cancel links ("" for the main app,
    "/try/s/<sandbox>" for a playground sandbox). Every value is HTML-escaped."""
    t = html.escape(token)
    b = html.escape(base)
    cancel_q = html.escape(order.get("cancel_query") or "")
    cancel_href = f"{b}/paypal/cancel?token={t}" + (f"&amp;{cancel_q}" if cancel_q else "")
    extra = f'<p class="hint">{html.escape(note)}</p>' if note else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Mock PayPal checkout</title>
<link rel="stylesheet" href="/static/app.css"></head><body class="checkout">
<div class="card"><div class="brand">Mock checkout <span>stands in for the PayPal approval page</span></div>
<p>{html.escape(order['description'])}</p><p class="amount">{html.escape(order['amount'])} {html.escape(order['currency'])}</p>
<a class="btn primary" href="{b}/paypal/mock-approve?token={t}">Pay now</a>
<a class="btn" href="{cancel_href}">Cancel and return</a>{extra}</div></body></html>"""
