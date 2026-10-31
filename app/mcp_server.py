"""GuardRail Buyer as an MCP server (stdio): any MCP agent can shop, only through the guardrails.

    python -m app.mcp_server                                  # embedded offline mode (no keys)
    GRB_API_URL=http://localhost:8000 python -m app.mcp_server  # remote mode (recommended)

Model Context Protocol over stdio: newline-delimited JSON-RPC 2.0 on stdin/stdout, logs on
stderr. Implements `initialize`, `ping`, `tools/list` and `tools/call` (protocol revisions
2025-06-18, 2025-03-26 and 2024-11-05). No SDK dependency, so it runs from the pinned
requirements without network access.

Modes
* remote (GRB_API_URL set): every tool call is forwarded to the running GuardRail server
  (`POST /tools/{name}`), where `GuardedTools` validates it and `BuyerService` enforces the
  policy. This process holds no PayPal credentials and no approver token; approvals happen
  in the dashboard, payments on PayPal (or the local mock checkout).
* embedded (default): a self-contained offline sandbox in this process with the local mock
  PayPal (no network, no key; ALLOW orders are captured at once, there is no buyer step) and an
  in-memory ledger (MCP_DB_PATH=<file> to persist).
  Real PayPal is refused here on purpose: use remote mode so credentials stay in the server.

In both modes the tool set has no approve / reject / capture / cancel tool, accepts only
catalog product ids and quantities (never prices, merchants or payees), and every
money-moving call passes the same server-side policy gate as the built-in agent.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Any, Callable

os.environ.setdefault("GRB_NO_AUTOAPP", "1")
os.environ.setdefault("GRB_NO_DOTENV", "1")

SERVER_NAME = "guardrail-buyer"
SERVER_VERSION = "0.5.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
# Credentials an MCP process must never hold: removed from this process's environment at start.
SCRUBBED_ENV = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_ACCESS_TOKEN", "APPROVER_TOKEN",
                "POLICY_OWNER_TOKEN", "STATE_SECRET", "NEXCONN_APP_SECRET")
INSTRUCTIONS = (
    "Purchasing tools gated by a server-side spend policy (per-order cap, daily cap, hard maximum, "
    "merchant/category allowlists). Always search_catalog first and use the returned product_id; you "
    "cannot set prices or merchants. Call check_policy, then create_order. NEEDS_APPROVAL -> "
    "request_approval with the user's stated purpose; a human decides in the dashboard. DENIED is final: "
    "do not retry, split or work around it. AWAITING_PAYER_APPROVAL means the order exists but nothing is "
    "paid until the buyer approves at approve_url; only ORDER_PLACED is bought.")


def log(msg: str) -> None:
    print(f"[guardrail-mcp] {msg}", file=sys.stderr, flush=True)


# ---- backends -----------------------------------------------------------------
class EmbeddedBackend:
    """Offline sandbox: BuyerService + MockPayPal in this process."""
    mode = "embedded"

    def __init__(self, db_path: str | None = None):
        from pathlib import Path

        from .guardrails import Policy
        from .paypal_client import MockPayPal
        from .service import BuyerService, Catalog
        from .tool_gateway import GuardedTools

        if os.environ.get("PAYPAL_MODE", "mock").lower() != "mock":
            raise SystemExit("ERROR: the embedded MCP mode is offline-only (mock PayPal). To use the real "
                             "PayPal Sandbox, run the GuardRail server and set GRB_API_URL, so PayPal "
                             "credentials stay in the server and never in the MCP process.")
        root = Path(__file__).resolve().parent.parent
        policy = Policy.load(os.environ.get("POLICY_PATH", root / "policy.yaml"))
        catalog = Catalog.load(os.environ.get("CATALOG_PATH", root / "data" / "catalog.json"))
        # Self-contained: the mock captures at once. There is no checkout page in this process,
        # so PAYPAL_MOCK_APPROVAL is ignored here (use remote mode for the buyer-approval round trip).
        paypal = MockPayPal(require_payer_approval=False)
        self.service = BuyerService(policy, catalog, paypal, db_path or os.environ.get("MCP_DB_PATH", ":memory:"))
        self.service.invalidate_lost_mock_orders()  # takes the single-process mock lock for a file DB
        self.tools = GuardedTools(self.service)

    def call(self, name: str, args: Any, session_id: str) -> dict[str, Any]:
        return self.tools.call(name, args, session_id)

    def close(self) -> None:
        self.service.release_mock_lock()


class RemoteBackend:
    """Forward tool calls to a running GuardRail server; the policy runs there."""
    mode = "remote"

    def __init__(self, base_url: str, client: Any = None, timeout: float = 60.0):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.httpx = httpx
        # trust_env=False: the GuardRail server is usually local; never route it via a proxy
        self.client = client or httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False)

    def call(self, name: str, args: Any, session_id: str) -> dict[str, Any]:
        try:
            r = self.client.post(f"/tools/{name}", json={"arguments": args if args is not None else {},
                                                          "session_id": session_id})
        except self.httpx.HTTPError as e:
            return {"ok": False, "code": "UNAVAILABLE",
                    "error": f"GuardRail server unreachable at {self.base_url} ({type(e).__name__})"}
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code == 200 and isinstance(body, dict) and "ok" in body:
            return body
        detail = body.get("detail") if isinstance(body, dict) else None
        if not isinstance(detail, str):
            detail = json.dumps(detail)[:200] if detail is not None else r.reason_phrase
        return {"ok": False, "code": f"HTTP_{r.status_code}",
                "error": f"GuardRail server answered {r.status_code}: {detail}"[:300]}

    def close(self) -> None:
        self.client.close()


# ---- JSON-RPC / MCP -------------------------------------------------------------
class MCPServer:
    def __init__(self, backend: Any, session_id: str | None = None):
        from .tool_gateway import TOOL_NAMES, TOOL_SPECS

        self.backend = backend
        self.session_id = session_id or f"mcp-{secrets.token_hex(8)}"
        self.tool_specs = TOOL_SPECS
        self.tool_names = set(TOOL_NAMES)
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "initialize": self._initialize, "ping": lambda p: {},
            "tools/list": self._tools_list, "tools/call": self._tools_call}

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "GuardRail Buyer", "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS}

    def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": self.tool_specs}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if name not in self.tool_names:
            raise RPCError(-32602, f"unknown tool: {str(name)[:64]}")
        out = self.backend.call(name, params.get("arguments"), self.session_id)
        if out.get("ok"):
            result = out["result"]
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}],
                    "structuredContent": result, "isError": False}
        err = {"error": out.get("error", "refused"), "code": out.get("code")}
        return {"content": [{"type": "text", "text": json.dumps(err)}], "structuredContent": err,
                "isError": True}

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """One JSON-RPC message -> response (None for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return None  # a response to a request we never send; ignore
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        mid, is_note = msg.get("id"), "id" not in msg
        params = msg.get("params") or {}
        handler = self.handlers.get(msg["method"])
        if is_note:
            return None  # notifications/initialized, notifications/cancelled, ...
        if handler is None:
            return _error(mid, -32601, f"method not found: {msg['method'][:64]}")
        if not isinstance(params, dict):
            return _error(mid, -32602, "params must be an object")
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": handler(params)}
        except RPCError as e:
            return _error(mid, e.code, e.message)
        except Exception as e:  # never crash the session, never leak internals
            log(f"internal error in {msg['method']}: {type(e).__name__}")
            return _error(mid, -32603, f"internal error: {type(e).__name__}")

    def handle_line(self, line: str) -> list[dict[str, Any]]:
        try:
            msg = json.loads(line)
        except ValueError:
            return [_error(None, -32700, "parse error")]
        if isinstance(msg, list):  # JSON-RPC batch (2025-03-26)
            if not msg:
                return [_error(None, -32600, "empty batch")]
            return [r for r in (self.handle(m) for m in msg) if r is not None]
        r = self.handle(msg)
        return [r] if r is not None else []

    def serve(self, stdin=None, stdout=None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            for resp in self.handle_line(line):
                stdout.write(json.dumps(resp, separators=(",", ":"), default=str) + "\n")
                stdout.flush()


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def scrub_credentials() -> list[str]:
    removed = [k for k in SCRUBBED_ENV if os.environ.pop(k, None) is not None]
    if removed:
        log(f"removed {', '.join(removed)} from this process: the MCP server never needs PayPal "
            f"credentials or the approver token (they belong to the GuardRail server)")
    return removed


def make_backend() -> Any:
    url = os.environ.get("GRB_API_URL", "").strip()
    return RemoteBackend(url) if url else EmbeddedBackend()


def main() -> int:
    scrub_credentials()
    from .service import MockLockError

    try:
        backend = make_backend()
    except MockLockError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    server = MCPServer(backend)
    log(f"ready ({backend.mode} mode{', ' + backend.base_url if backend.mode == 'remote' else ''}; "
        f"session {server.session_id})")
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
