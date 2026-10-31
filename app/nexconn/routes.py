"""HTTP routes for the chat approval channel (mounted by app/main.py when NEXCONN_MODE != off).

    GET  /chat                     the approver's chat page (Nexconn Web Chat SDK, or the mock)
    GET  /nexconn/config           public settings for the page (never the App Secret)
    POST /nexconn/session          approver token -> Chat SDK access token for one approver
    POST /nexconn/webhook          live: signed Nexconn `message:send` deliveries (decisions)
    POST /nexconn/relay            live + NEXCONN_RELAY=1: the page forwards a decision it sent
                                   (for a local run without a public webhook URL)
    GET  /nexconn/status           approver token: what was posted / received
    POST /nexconn/retry            approver token: re-send posts that failed
    GET  /nexconn/sdk/{chat,engine}.mjs  redirect to the pinned SDK build (import map targets)
    GET  /nexconn/mock/messages    mock: the room's messages (mock session token)
    POST /nexconn/mock/send        mock: send as the session's user (runs the webhook path)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from .channel import ChatApprovalChannel, InboundBusy
from .client import MockNexconn, NexconnError, text_content
from .config import SDK_VERSION
from .protocol import APPROVAL_ID
from .webhook import WebhookRejected, WebhookVerifier, message_events


class SendBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    extra: dict[str, Any] | None = None


class RelayBody(BaseModel):
    message_id: str = Field(..., min_length=1, max_length=128)
    user_id: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=1, max_length=500)
    extra: dict[str, Any] | None = None


def build_router(channel: ChatApprovalChannel, require_approver, static_dir, relay: bool = False,
                 approver_auth: bool = True, route_item=None) -> APIRouter:
    """`route_item(item, source)`: optional dispatch for signed webhook items (the /try playground
    routes its own groups' messages); default: every item goes to `channel.handle_message`, which
    ignores other channels."""
    cfg = channel.cfg
    route = route_item or channel.handle_message
    r = APIRouter()

    def require_real_approver():
        # An open demo (DEMO_MODE=1, no APPROVER_TOKEN) must never hand out a real Nexconn approver
        # identity or accept relayed decisions: in live mode both need a configured token.
        if cfg.mode == "live" and not approver_auth:
            raise HTTPException(503, "NEXCONN_MODE=live needs APPROVER_TOKEN set (an open DEMO_MODE cannot "
                                     "issue Nexconn approver tokens or relay decisions)")
    verifier = WebhookVerifier(cfg.app_key, cfg.app_secret, cfg.webhook_max_skew_s) \
        if cfg.mode == "live" else None

    @r.get("/chat", include_in_schema=False)
    def chat_page():
        return FileResponse(static_dir / "chat.html")

    @r.get("/nexconn/config")
    def config():
        return {**cfg.public(), "relay": relay, "app_key": cfg.app_key if cfg.mode == "live" else ""}

    @r.get("/nexconn/sdk/{name}.mjs", include_in_schema=False)
    def sdk(name: str):
        if name not in ("chat", "engine"):
            raise HTTPException(404)
        return RedirectResponse(f"{cfg.sdk_base}/@nexconn/{name}@{SDK_VERSION}/index.mjs", status_code=302)

    @r.post("/nexconn/session", dependencies=[Depends(require_real_approver), Depends(require_approver)])
    def session(as_user: str | None = None):
        try:
            return channel.session(as_user)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except NexconnError as e:
            raise HTTPException(502, f"Nexconn: {e}")

    @r.get("/nexconn/status", dependencies=[Depends(require_approver)])
    def status():
        return channel.status()

    @r.post("/nexconn/retry", dependencies=[Depends(require_approver)])
    def retry():
        out = channel.recover()
        return {"requeued": out["posts"], "decisions_finished": out["decisions"]}

    @r.post("/nexconn/webhook")
    async def webhook(request: Request):
        if verifier is None:
            raise HTTPException(404, "the Nexconn webhook only exists in NEXCONN_MODE=live")
        body = await request.body()
        try:
            env, nonce = verifier.verify({k.lower(): v for k, v in request.headers.items()}, body)
        except WebhookRejected as e:
            raise HTTPException(e.status, e.reason)
        claim = await run_in_threadpool(channel.claim_nonce, nonce, verifier.window_s)
        if claim == "done":
            return {"ok": True, "duplicate_delivery": True}
        if claim == "busy":
            raise HTTPException(409, "this delivery is being processed; retry later")
        ok = False
        try:
            # handle_message may create a PayPal order: keep it off the event loop
            handled = [await run_in_threadpool(route, item, "webhook")
                       for item in message_events(env)]
            ok = True
        except InboundBusy:
            raise HTTPException(409, "a message in this delivery is being processed; retry later")
        finally:
            await run_in_threadpool(channel.finish_nonce, nonce, ok)
        return {"ok": True, "handled": handled}

    @r.post("/nexconn/relay", dependencies=[Depends(require_real_approver), Depends(require_approver)])
    def relay_decision(body: RelayBody):
        if not (relay and cfg.mode == "live"):
            raise HTTPException(404, "relay is off (NEXCONN_MODE=live with NEXCONN_RELAY=1 enables it)")
        try:
            return channel.handle_message(_item(cfg.group_id, body.user_id, body.message_id, body.text,
                                                body.extra), "relay")
        except InboundBusy:
            raise HTTPException(409, "this decision is being processed; retry later")

    # ---- mock transport (offline demo and tests) ---------------------------------
    def mock_user(x_nexconn_token: str | None) -> str:
        if not isinstance(channel.client, MockNexconn):
            raise HTTPException(404, "mock transport only exists in NEXCONN_MODE=mock")
        uid = channel.client.user_for_token(x_nexconn_token or "")
        if uid is None:
            raise HTTPException(401, "mock session token required (X-Nexconn-Token)")
        return uid

    @r.get("/nexconn/mock/messages")
    def mock_messages(after: int = 0, x_nexconn_token: str | None = Header(default=None)):
        mock_user(x_nexconn_token)
        after = max(0, min(after, 1_000_000))
        msgs = channel.client.history(cfg.group_id, after)
        return {"messages": msgs, "next": after + len(msgs)}

    @r.post("/nexconn/mock/send")
    def mock_send(body: SendBody, x_nexconn_token: str | None = Header(default=None)):
        uid = mock_user(x_nexconn_token)
        channel.bootstrap()
        extra = _clean_extra(body.extra)
        try:
            mid = channel.client.send_group_text(uid, cfg.group_id, body.text, extra)
        except NexconnError as e:
            raise HTTPException(502, str(e))
        # what a `message:send` webhook would carry for this message
        return {"message_id": mid,
                "handled": channel.handle_message(_item(cfg.group_id, uid, mid, body.text, extra), "mock")}

    return r


def _clean_extra(extra: dict | None) -> dict | None:
    """Only a structured decision travels in `extra`; anything else is dropped."""
    if extra is None or extra.get("grb") != "decision" or not (
            isinstance(extra.get("approval_id"), str) and APPROVAL_ID.fullmatch(extra["approval_id"])):
        return None
    return {"grb": "decision", "approval_id": extra["approval_id"], "decision": extra.get("decision")}


def _item(group_id: str, user_id: str, message_id: str, text: str, extra: dict | None) -> dict[str, Any]:
    extra = _clean_extra(extra)
    return {"userId": user_id, "channelId": group_id, "channelType": 3, "messageType": "RC:TxtMsg",
            "messageId": message_id, "content": text_content(text, extra), "os": "Websocket"}
