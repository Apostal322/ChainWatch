"""Edge API on the collector VM: read-only /v1/live/* over the local SQLite file.

* Bound to 127.0.0.1 and published only through a TLS-terminating reverse proxy (Caddy),
  so the database is never exposed and traffic is HTTPS.
* Every /v1/live route requires ``Authorization: Bearer <token>``; the token comes from the
  environment (``live.api_token_env``), is at least 32 characters and is never in a file
  under version control.
* Per-client rate limit, 64 KB body limit, GET-only routes, no API docs, security headers.
* Imports only the live package and FastAPI: no pandas, scikit-learn or XGBoost, and no
  code path can train a model.
"""

from __future__ import annotations

import dataclasses
import hmac
import os
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from chainwatch import __version__
from chainwatch.config import LiveConfig
from chainwatch.live.store import LiveReader, NoLiveData
from chainwatch.serving.live_api import connection_state, install_live_routes
from chainwatch.serving.security import (
    SECURITY_HEADERS,
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    request_id,
)

MIN_TOKEN_CHARS = 32


def _error(status: int, code: str, message: str, request: Request) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": None}, "request_id": rid},
    )


def edge_token(live: LiveConfig) -> str:
    token = os.environ.get(live.api_token_env, "")
    if len(token) < MIN_TOKEN_CHARS:
        raise SystemExit(
            f"${live.api_token_env} must be set to a random token of at least "
            f"{MIN_TOKEN_CHARS} characters (e.g. `openssl rand -hex 32`)"
        )
    return token


def create_edge_app(live: LiveConfig, token: str) -> FastAPI:
    if len(token) < MIN_TOKEN_CHARS:
        raise ValueError("edge API token too short")
    live = dataclasses.replace(live, enabled=True)  # the edge API exists only to serve live data
    app = FastAPI(
        title="ChainWatch live edge API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    reader = LiveReader(live.db_path)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {400: "bad_request", 404: "not_found", 405: "method_not_allowed"}
        return _error(
            exc.status_code, code.get(exc.status_code, "http_error"), str(exc.detail), request
        )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        request.state.request_id = request_id(request.headers.get("x-request-id"))
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        response.headers["cache-control"] = "no-store"
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    @app.get("/health/live", include_in_schema=False)
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def health_ready() -> Any:
        """Unauthenticated liveness of COLLECTION: 200 only if the collector heartbeat is
        fresh and it is connected. Reveals nothing but these two facts."""
        try:
            state = connection_state(reader.status(), time.time(), live.heartbeat_s)
        except NoLiveData:
            state = "disconnected"
        ok = state == "connected"
        return JSONResponse({"status": "ready" if ok else state}, status_code=200 if ok else 503)

    install_live_routes(
        app, live, _error, token=token, trusted_proxy_hops=live.edge_trusted_proxy_hops
    )
    expected = f"bearer {token}".encode()

    def authenticated(scope) -> bool:
        """The service token holder (the dashboard) is not rate limited here, so floods of
        unauthenticated requests from a shared egress IP cannot lock it out."""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                given = value[:7].lower() + value[7:]
                return hmac.compare_digest(given, expected)
        return False

    app.add_middleware(
        RateLimitMiddleware,
        per_minute=live.edge_rate_limit_per_minute,
        trusted_proxy_hops=live.edge_trusted_proxy_hops,
        exempt=authenticated,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=65_536)
    return app
