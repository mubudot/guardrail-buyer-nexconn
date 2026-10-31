"""HTTP routes of the /try playground (mounted by app/main.py when TRY_MODE=on).

    GET  /try, /try/join/{code}    the playground page (the page reads the invite code from the path)
    POST /try/start                {name?} -> a new sandbox: session secret, member, invite URL, chat mode
    POST /try/join                 {code, name?} -> join a sandbox as a teammate (second approver)
    GET  /try/state                X-Try-Session: the sandbox (members, limits, approvals, orders, steps)
    POST /try/session              X-Try-Session: Chat SDK session for this member (no new registration)
    POST /try/agent                X-Try-Session: {request} -> the rule-based offline agent's answer
    POST /try/reset                X-Try-Session (owner): clear orders, approvals and spend
    GET  /try/mock/messages        offline stand-in room (X-Nexconn-Token), same contract as /nexconn/mock/*
    POST /try/mock/send
    GET  /try/s/{sid}/paypal/...   the sandbox's simulated checkout (mock-checkout, mock-approve, return, cancel)

Creating or joining needs a POST from the page (a button), so link previews and crawlers never
register Nexconn users.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..checkout import mock_checkout_page
from ..nexconn.routes import SendBody
from ..ratelimit import client_ip
from .manager import PlaygroundError, PlaygroundManager

_SID = re.compile(r"[0-9a-f]{12}")


class StartBody(BaseModel):
    name: str | None = Field(default=None, max_length=64)


class JoinBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=64)


class TryAgentBody(BaseModel):
    request: str = Field(..., min_length=1, max_length=500)


def build_router(manager: PlaygroundManager, static_dir) -> APIRouter:
    r = APIRouter()

    async def call(fn, *args) -> Any:
        try:
            return await run_in_threadpool(fn, *args)
        except PlaygroundError as e:
            raise HTTPException(e.status, e.detail, headers=e.headers) from None

    def sid_ok(sid: str) -> str:
        if not _SID.fullmatch(sid):
            raise HTTPException(404, "unknown sandbox")
        return sid

    @r.get("/try", include_in_schema=False)
    def page():
        return FileResponse(static_dir / "try.html", headers={"Cache-Control": "no-cache"})

    @r.get("/try/join/{code}", include_in_schema=False)
    def invite_page(code: str):
        return FileResponse(static_dir / "try.html", headers={"Cache-Control": "no-cache",
                                                               "X-Robots-Tag": "noindex"})

    @r.post("/try/start")
    async def start(body: StartBody, request: Request):
        return await call(manager.create, client_ip(request), body.name)

    @r.post("/try/join")
    async def join(body: JoinBody, request: Request):
        return await call(manager.join, client_ip(request), body.code, body.name)

    @r.get("/try/state")
    async def state(x_try_session: str | None = Header(default=None)):
        return await call(manager.state, x_try_session)

    @r.post("/try/session")
    async def session(x_try_session: str | None = Header(default=None)):
        return await call(manager.session, x_try_session)

    @r.post("/try/agent")
    async def agent(body: TryAgentBody, request: Request, x_try_session: str | None = Header(default=None)):
        return await call(manager.agent, client_ip(request), x_try_session, body.request)

    @r.post("/try/reset")
    async def reset(x_try_session: str | None = Header(default=None)):
        return await call(manager.reset, x_try_session)

    # ---- offline stand-in room -----------------------------------------------------------
    @r.get("/try/mock/messages")
    async def mock_messages(after: int = 0, x_nexconn_token: str | None = Header(default=None)):
        return await call(manager.mock_messages, x_nexconn_token, after)

    @r.post("/try/mock/send")
    async def mock_send(body: SendBody, x_nexconn_token: str | None = Header(default=None)):
        return await call(manager.mock_send, x_nexconn_token, body.text, body.extra)

    # ---- simulated checkout ----------------------------------------------------------------
    @r.get("/try/s/{sid}/paypal/mock-checkout", response_class=HTMLResponse, include_in_schema=False)
    async def checkout(sid: str, token: str):
        order = await call(manager.checkout_order, sid_ok(sid), token)
        return mock_checkout_page(token, order, base=f"/try/s/{sid}",
                                  note="GuardRail playground: simulated payment, no real money moves.")

    @r.get("/try/s/{sid}/paypal/mock-approve", include_in_schema=False)
    async def checkout_approve(sid: str, token: str):
        await call(manager.checkout_approve, sid_ok(sid), token)
        return RedirectResponse(f"/try/s/{sid}/paypal/return?token={quote(token)}&PayerID=MOCKPAYER", status_code=303)

    @r.get("/try/s/{sid}/paypal/return", include_in_schema=False)
    async def checkout_return(sid: str, token: str, PayerID: str | None = None):
        order = await call(manager.checkout_return, sid_ok(sid), token)
        return RedirectResponse(f"/try?paid={quote(order['id'])}&status={quote(order['status'])}", status_code=303)

    @r.get("/try/s/{sid}/paypal/cancel", include_in_schema=False)
    async def checkout_cancel(sid: str, token: str, state: str = ""):
        order = await call(manager.checkout_cancel, sid_ok(sid), token, state)
        if order["result"] in ("CANCELLED", "ALREADY_CANCELLED"):
            return RedirectResponse(f"/try?cancelled={quote(order['id'])}", status_code=303)
        return RedirectResponse(f"/try?paid={quote(order['id'])}&status={quote(order['status'])}", status_code=303)

    return r
