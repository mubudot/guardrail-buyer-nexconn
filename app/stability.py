"""Liveness / readiness probes and the catch-all error handler.

    GET /health/live    200 {"ok": true} whenever the process answers (no dependencies)
    GET /health/ready   200 when the main database answers, the Nexconn channel (if on) is ready and
                        the playground registry (if on) answers; else 503 naming the failed check

Unexpected exceptions become a JSON 500 that names a reference (logged with the traceback) and
reveals nothing else; HTTPException and validation errors keep FastAPI's usual answers.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("guardrail")


def install_error_handler(app: FastAPI) -> None:
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        ref = secrets.token_hex(4)
        log.error("unexpected error %s on %s %s", ref, request.method, request.url.path,
                  exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse({"detail": f"Something went wrong on our side; nothing was bought or approved. "
                                       f"Reference {ref}"}, status_code=500)

    app.add_exception_handler(Exception, unexpected)


def build_health_router(svc, chat=None, playground=None) -> APIRouter:
    r = APIRouter()

    @r.get("/health/live")
    def live():
        return {"ok": True}

    @r.get("/health/ready")
    def ready():
        checks: dict[str, bool] = {}
        try:
            with svc.store.lock:
                checks["database"] = svc.store.conn.execute("SELECT 1").fetchone()[0] == 1
        except Exception:
            checks["database"] = False
        if chat is not None:
            checks["nexconn"] = bool(chat.ready)
        if playground is not None:
            try:
                checks["playground"] = playground.ready()
            except Exception:
                checks["playground"] = False
        failed = [k for k, ok in checks.items() if not ok]
        body = {"ok": not failed, "checks": checks, **({"failed": failed} if failed else {})}
        return JSONResponse(body, status_code=503 if failed else 200)

    return r
