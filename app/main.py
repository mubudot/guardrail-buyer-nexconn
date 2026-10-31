"""FastAPI app for GuardRail Buyer: REST API, PayPal return/cancel, dashboard."""
from __future__ import annotations

import hmac
import logging
import os
import threading
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
# Auto-load .env (never overrides variables already set in the environment).
if os.environ.get("GRB_NO_DOTENV") != "1":
    load_dotenv(ROOT / ".env", override=False)

from .agent import LLMError, PurchasingAgent  # noqa: E402
from .offline_agent import OfflineAgent, make_agent  # noqa: E402
from .guardrails import Policy  # noqa: E402
from .paypal_client import MockPayPal, PayPalError, make_client  # noqa: E402
from .service import BuyerService, Catalog, MockLockError, PolicyConflict  # noqa: E402
from .tool_gateway import TOOL_NAMES, TOOL_SPECS, GuardedTools  # noqa: E402
from .nexconn import NexconnConfig, NexconnConfigError, build_channel, relay_enabled  # noqa: E402
from .nexconn.routes import build_router as build_nexconn_router  # noqa: E402
from .checkout import mock_checkout_page  # noqa: E402
from .nexconn.client import MockNexconn  # noqa: E402
from .playground import PlaygroundConfig, PlaygroundManager  # noqa: E402
from .playground.routes import build_router as build_try_router  # noqa: E402
from .stability import build_health_router, install_error_handler  # noqa: E402
from .ratelimit import ConfigError, RateLimiter, client_ip, parse_rate_limit  # noqa: E402,F401  (re-exported)

log = logging.getLogger("guardrail")
STATIC = ROOT / "app" / "static"


class ToolCall(BaseModel):
    """One guarded tool call from an external agent (the MCP server in remote mode)."""
    arguments: dict = Field(default_factory=dict)
    session_id: str | None = Field(default=None, max_length=64)


class AgentRequest(BaseModel):
    request: str = Field(..., min_length=1, max_length=2000,
                         examples=["Buy 2 USB-C cables and a wireless mouse for the new hire"])


def build_service(db_path: str | None = None, paypal_mode: str | None = None) -> BuyerService:
    policy = Policy.load(os.environ.get("POLICY_PATH", ROOT / "policy.yaml"))
    catalog = Catalog.load(os.environ.get("CATALOG_PATH", ROOT / "data" / "catalog.json"))
    return BuyerService(policy, catalog, make_client(paypal_mode),
                        db_path or os.environ.get("DB_PATH", str(ROOT / "guardrail.db")))


def create_app(service: BuyerService | None = None, agent: PurchasingAgent | None = None,
               approver_token: str | None = None, demo_mode: bool | None = None,
               agent_rate_limit: tuple[int, float] | None | str = "env",
               tools_rate_limit: tuple[int, float] | None | str = "env",
               nexconn: object = "env") -> FastAPI:
    """`nexconn`: "env" builds the chat approval channel from NEXCONN_* (off by default);
    tests pass a ready ChatApprovalChannel, or None for no channel."""
    app = FastAPI(title="GuardRail Buyer", version="0.4.0",
                  description="AI purchasing agent with PayPal spend guardrails (Sandbox)")
    # validate configuration before touching the database (fail fast at startup)
    limit = parse_rate_limit(os.environ.get("AGENT_RATE_LIMIT")) if agent_rate_limit == "env" else agent_rate_limit
    limiter = RateLimiter(*limit) if limit else None
    tools_limit = parse_rate_limit(os.environ.get("TOOLS_RATE_LIMIT", "120/60"), "TOOLS_RATE_LIMIT") \
        if tools_rate_limit == "env" else tools_rate_limit
    tools_limiter = RateLimiter(*tools_limit) if tools_limit else None
    try:
        nexconn_cfg = NexconnConfig.from_env() if nexconn == "env" else None
    except NexconnConfigError as e:
        raise ConfigError(str(e)) from None
    chat_mode = nexconn_cfg.mode if nexconn == "env" else (nexconn.cfg.mode if nexconn is not None else "off")
    try_cfg = PlaygroundConfig.from_env(nexconn_mode=chat_mode)
    main_group = nexconn_cfg.group_id if nexconn == "env" and nexconn_cfg else getattr(getattr(nexconn, "cfg", None), "group_id", "")
    if try_cfg.enabled and main_group.startswith("grbtry"):
        raise ConfigError("NEXCONN_GROUP_ID must not start with 'grbtry' (reserved for /try playground rooms)")
    svc = service or build_service()
    app.state.service = svc
    # Mock mode is single-process (N3): take the exclusive DB lock first (fails fast if
    # another process holds it), only then invalidate mock orders lost on restart (B9).
    lost = svc.invalidate_lost_mock_orders()
    if lost:
        log.warning("mock PayPal state was reset: %d open mock order(s) marked MOCK_RESET", len(lost))
    app.state.agent = agent or make_agent(svc)
    token = approver_token if approver_token is not None else os.environ.get("APPROVER_TOKEN", "")
    demo = demo_mode if demo_mode is not None else os.environ.get("DEMO_MODE") == "1"
    if not token:
        if demo:
            log.warning("APPROVER_TOKEN empty and DEMO_MODE=1: approve/reject/cancel are OPEN (local demo only)")
        else:
            log.warning("APPROVER_TOKEN not set: approve/reject/cancel are REFUSED (503). "
                        "Set APPROVER_TOKEN, or DEMO_MODE=1 for an open local demo.")
    approver_mode = "token" if token else ("open-demo" if demo else "disabled")
    # Policy edits need the owner token (POLICY_OWNER_TOKEN; falls back to APPROVER_TOKEN when unset).
    owner_token = os.environ.get("POLICY_OWNER_TOKEN", "") or token
    owner_mode = ("owner-token" if os.environ.get("POLICY_OWNER_TOKEN") else "approver-token") if owner_token \
        else ("open-demo" if demo else "disabled")

    def rate_limited(request: Request):
        if limiter is None:
            return
        wait = limiter.check(request.client.host if request.client else "unknown")
        if wait is not None:
            raise HTTPException(429, f"rate limit: at most {limiter.limit} agent requests per "
                                     f"{limiter.per:g}s per client", headers={"Retry-After": str(int(wait) + 1)})

    def tools_rate_limited(request: Request):
        if tools_limiter is None:
            return
        wait = tools_limiter.check(request.client.host if request.client else "unknown")
        if wait is not None:
            raise HTTPException(429, f"rate limit: at most {tools_limiter.limit} tool calls per "
                                     f"{tools_limiter.per:g}s per client",
                                headers={"Retry-After": str(int(wait) + 1)})

    def require_approver(x_approver_token: str | None = Header(default=None),
                         authorization: str | None = Header(default=None)):
        if not token:
            if demo:
                return
            raise HTTPException(503, "APPROVER_TOKEN is not configured; approve/reject/cancel are disabled "
                                     "(set APPROVER_TOKEN, or DEMO_MODE=1 for an open local demo)")
        given = x_approver_token or ""
        if not given and authorization and authorization.lower().startswith("bearer "):
            given = authorization[7:]
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "approver token required (X-Approver-Token header)")

    def require_owner(x_owner_token: str | None = Header(default=None)):
        if not owner_token:
            if demo:
                return
            raise HTTPException(503, "no POLICY_OWNER_TOKEN / APPROVER_TOKEN configured; policy editing is "
                                     "disabled (or set DEMO_MODE=1 for an open local demo)")
        if not hmac.compare_digest((x_owner_token or "").encode(), owner_token.encode()):
            raise HTTPException(401, "policy owner token required (X-Owner-Token header)")

    # ---- Nexconn chat approval channel (off unless NEXCONN_MODE is set) ----
    chat = build_channel(svc, nexconn_cfg) if nexconn == "env" else nexconn
    app.state.nexconn = chat
    if chat is not None:
        def route_item(item, source):   # playground rooms are routed to their own sandbox
            pg = app.state.playground
            if pg is not None and pg.owns(item):
                return pg.handle_message(item, source)
            return chat.handle_message(item, source)
        app.include_router(build_nexconn_router(chat, require_approver, STATIC, relay_enabled(),
                                                approver_auth=bool(token), route_item=route_item))
        if chat.cfg.mode == "live":   # register users + group, re-send unsent posts, without delaying startup
            threading.Thread(target=chat.start, name="nexconn-bootstrap", daemon=True).start()
        else:
            chat.start()
        app.router.on_shutdown.append(lambda: chat.drain(5.0))

    # ---- /try playground (off unless TRY_MODE=on; needs the chat channel) ----
    playground = None
    if try_cfg.enabled:
        playground = PlaygroundManager(
            try_cfg, chat.cfg, live_client=chat.client if chat.cfg.mode == "live" else None,
            mock_client=MockNexconn(), catalog=svc.catalog,
            policy=Policy.load(os.environ.get("POLICY_PATH", ROOT / "policy.yaml")))
        app.include_router(build_try_router(playground, STATIC))
        app.router.on_shutdown.append(playground.close)
    app.state.playground = playground
    install_error_handler(app)
    app.include_router(build_health_router(svc, chat, playground))

    # ---- dashboard -----------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "index.html")

    # ---- API -----------------------------------------------------------
    @app.get("/health")
    def health():
        return {"ok": True, "paypal": svc.paypal.name, "model": app.state.agent.model,
                "agent_mode": "offline" if isinstance(app.state.agent, OfflineAgent) else "claude",
                "approver_auth": bool(token), "approver_mode": approver_mode, "policy_owner_mode": owner_mode,
                "agent_rate_limit": f"{limiter.limit}/{limiter.per:g}s" if limiter else None,
                "tools_rate_limit": f"{tools_limiter.limit}/{tools_limiter.per:g}s" if tools_limiter else None,
                "instance": os.environ.get("GRB_INSTANCE"),
                "nexconn": ({**chat.cfg.public(), "ready": chat.ready} if chat is not None else {"mode": "off"}),
                "playground": playground.health() if playground is not None else {"mode": "off"}}

    @app.get("/policy")
    def get_policy():
        pol, revision = svc.policy_snapshot()  # fields and revision from one snapshot
        return {**pol, "spent_today": str(svc.spent_today()), "day": svc._today(), "revision": revision}

    @app.put("/policy", dependencies=[Depends(require_owner)])
    def put_policy(body: dict, if_match: str | None = Header(default=None)):
        """Owner edit of the caps / allowlists (X-Owner-Token). Validated server-side; only
        per_order_cap, daily_cap, hard_max_per_order, allowed_categories, allowed_merchants and
        blocked_merchants are editable. Invalid -> 422 with the reason; nothing changes.
        Optimistic concurrency: send the `revision` from GET /policy as `If-Match` (the
        dashboard does); a policy changed meanwhile -> 409, nothing changes."""
        try:
            return svc.update_policy(body, base_revision=if_match.strip('"') if if_match else None)
        except PolicyConflict as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/policy/history")
    def get_policy_history():
        return svc.policy_history()

    @app.get("/spend")
    def get_spend():
        return svc.spend_summary()

    @app.get("/spend/history")
    def get_spend_history(days: int = 14):
        try:
            return svc.spend_history(days)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/catalog")
    def get_catalog(q: str = "", category: str | None = None):
        return svc.catalog.search(q, category, limit=100)

    @app.post("/agent/request", dependencies=[Depends(rate_limited)])
    def agent_request(body: AgentRequest):
        try:
            return app.state.agent.run(body.request).to_dict()
        except LLMError as e:
            raise HTTPException(502, str(e))

    @app.get("/orders")
    def list_orders(status: str | None = None):
        svc.expire_stale()
        return svc.store.list("orders", status)

    @app.get("/approvals")
    def list_approvals(status: str | None = None):
        return svc.store.list("approvals", status)

    @app.get("/decisions")
    def list_decisions(limit: int = 1000, before_id: str | None = None):
        """Audit log: every guardrail decision (check / order / approval request / approval
        execution), newest first, with the current status of the order / approval it led
        to. Paged: `limit` (1..5000) rows, `before_id` = cursor (fetch decisions older than
        that one; use `next_before_id`). `total` is the number of decisions stored."""
        try:
            return svc.decisions_page(limit, before_id)
        except KeyError:
            raise HTTPException(404, "unknown before_id")

    @app.get("/decisions/{decision_id}")
    def get_decision(decision_id: str):
        """One decision: quote, every rule evaluated, approval, and the order's payment
        timeline (status transitions)."""
        try:
            return svc.decision_detail(decision_id)
        except KeyError:
            raise HTTPException(404, "unknown decision")

    def _decide(approval_id: str, fn):
        try:
            out = fn(approval_id)
        except KeyError:
            raise HTTPException(404, f"approval {approval_id} not found")
        except ValueError as e:
            raise HTTPException(409, str(e))
        if out.get("result") == "QUOTE_CHANGED":
            raise HTTPException(409, "quote changed since approval was requested "
                                     f"(approved {out['approved_quote']['amount']}, now "
                                     f"{(out['current_quote'] or {}).get('amount', 'unavailable')}); "
                                     "nothing was bought, request a fresh approval")
        return out

    @app.post("/approvals/{approval_id}/approve", dependencies=[Depends(require_approver)])
    def approve(approval_id: str):
        return _decide(approval_id, svc.approve)

    @app.post("/approvals/{approval_id}/reject", dependencies=[Depends(require_approver)])
    def reject(approval_id: str):
        return _decide(approval_id, svc.reject)

    @app.get("/orders/{order_id}")
    def get_order(order_id: str, refresh: bool = True):
        """One order; refresh=true (default) also asks PayPal for the live status."""
        try:
            return svc.get_order(order_id, refresh=refresh)
        except KeyError:
            raise HTTPException(404, "unknown order")

    @app.post("/orders/{order_id}/capture")
    def capture_order(order_id: str):
        """Capture an order the buyer approved on PayPal (fallback if the return redirect
        was lost). Not approved yet -> 409 with PayPal's issue code; nothing changes."""
        try:
            out = svc.capture(order_id)
        except KeyError:
            raise HTTPException(404, "unknown order")
        except ValueError as e:
            raise HTTPException(409, str(e))
        if out.get("result") == "NOT_APPROVED_YET":
            raise HTTPException(409, {"result": "NOT_APPROVED_YET", "paypal_issue": out.get("paypal_issue"),
                                      "message": "the buyer has not approved this order on PayPal yet",
                                      "order": {k: v for k, v in out.items() if k not in ("error",)}})
        return out

    @app.post("/orders/{order_id}/reconcile")
    def reconcile_order(order_id: str):
        """Resolve an order whose payment outcome is unknown (NEEDS_RECONCILIATION) from
        PayPal's order status. Never creates a second charge."""
        try:
            return svc.reconcile(order_id)
        except KeyError:
            raise HTTPException(404, "unknown order")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/orders/{order_id}/cancel", dependencies=[Depends(require_approver)])
    def cancel_order(order_id: str):
        try:
            out = svc.cancel(order_id)
        except KeyError:
            raise HTTPException(404, "unknown order")
        if out["result"] == "NOT_CANCELLABLE":
            raise HTTPException(409, f"order {order_id} is {out['status']}; only orders awaiting "
                                     "payer approval can be cancelled")
        return out

    # ---- guarded tools for external agents (MCP server, remote mode) ---------
    tools = GuardedTools(svc)

    @app.get("/tools")
    def list_tools():
        """The guarded tool set (same schemas the MCP server publishes). There is no
        approve / capture / cancel tool: those stay with a human and the approver token."""
        return {"tools": TOOL_SPECS}

    @app.post("/tools/{name}", dependencies=[Depends(tools_rate_limited)])
    def call_tool(name: str, body: ToolCall):
        """Run one guarded tool through the server-side policy gate. Policy outcomes
        (DENIED, NEEDS_APPROVAL) are results (200, ok=true); invalid arguments, unknown
        ids and refusals are ok=false with an error. Unknown tool -> 404."""
        if name not in TOOL_NAMES:
            raise HTTPException(404, f"unknown tool {name}")
        return tools.call(name, body.arguments, body.session_id)

    # ---- PayPal buyer round trip ---------------------------------------
    # PayPal redirects the buyer to return_url?token=<paypal order id>&PayerID=...
    # after approval, or to cancel_url?token=<paypal order id> on cancel.
    @app.get("/paypal/return")
    def paypal_return(token: str, PayerID: str | None = None, format: str = "html"):
        try:
            order = svc.complete_payment(token)
        except KeyError:
            raise HTTPException(404, "unknown PayPal order")
        except ValueError as e:
            raise HTTPException(409, str(e))
        if format == "json":
            return order
        return RedirectResponse(f"/?paid={order['id']}&status={order['status']}", status_code=303)

    @app.get("/paypal/cancel")
    def paypal_cancel(token: str, state: str = "", format: str = "html"):
        """PayPal's cancel redirect. Requires the signed `state` (HMAC with the server
        secret) that was put into this order's cancel_url when it was created; the public
        PayPal order id alone is refused (403). Only an order still awaiting payer
        approval is cancelled (atomically); an order being captured, captured, or of
        unknown outcome is left untouched."""
        try:
            order = svc.cancel_payment(token, state)
        except KeyError:
            raise HTTPException(404, "unknown PayPal order")
        except PermissionError:
            raise HTTPException(403, "missing or invalid signed cancel state")
        if format == "json":
            return order
        if order["result"] in ("CANCELLED", "ALREADY_CANCELLED"):
            return RedirectResponse(f"/?cancelled={order['id']}", status_code=303)
        return RedirectResponse(f"/?paid={order['id']}&status={order['status']}", status_code=303)

    @app.get("/paypal/mock-checkout", response_class=HTMLResponse, include_in_schema=False)
    def mock_checkout(token: str):
        """Stand-in for the PayPal approval page when PAYPAL_MODE=mock + PAYPAL_MOCK_APPROVAL=1."""
        if not isinstance(svc.paypal, MockPayPal):
            raise HTTPException(404, "mock checkout only exists in mock mode")
        try:
            o = svc.paypal.get_order(token)["raw"]
        except PayPalError:
            raise HTTPException(404, "unknown mock order (mock PayPal state is in-memory and is "
                                     "reset when the server restarts; place a new order)")
        return mock_checkout_page(token, o)

    @app.get("/paypal/mock-approve", include_in_schema=False)
    def mock_approve(token: str):
        if not isinstance(svc.paypal, MockPayPal):
            raise HTTPException(404, "mock checkout only exists in mock mode")
        try:
            svc.paypal.approve(token)
        except PayPalError:
            raise HTTPException(404, "unknown mock order")
        return RedirectResponse(f"/paypal/return?token={quote(token)}&PayerID=MOCKPAYER", status_code=303)

    return app


app = None
if os.environ.get("GRB_NO_AUTOAPP") != "1":
    try:
        app = create_app()
    except (MockLockError, ConfigError) as e:  # second mock process on the DB / bad config: fail fast
        raise SystemExit(f"ERROR: {e}") from None
