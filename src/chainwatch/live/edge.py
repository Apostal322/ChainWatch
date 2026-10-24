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
import json
import logging
import os
import re
import sys
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
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
    internal_error_headers,
    request_id,
)

log = logging.getLogger("chainwatch.edge")

MIN_TOKEN_CHARS = 32


def _error(status: int, code: str, message: str, request: Request) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": None}, "request_id": rid},
    )


TOKEN_RE = re.compile(rf"[A-Za-z0-9._~+/=-]{{{MIN_TOKEN_CHARS},}}")


def valid_token(token: str) -> bool:
    """At least 32 visible URL-safe characters (34 spaces used to pass a length check, and
    no request could ever match them: HTTP strips header whitespace)."""
    return TOKEN_RE.fullmatch(token) is not None


def edge_tokens(live: LiveConfig) -> tuple[str, ...]:
    """The token, plus ``<env>_NEXT`` if set: during a rotation the edge accepts both, so the
    dashboard can switch without an outage (then the old one is removed)."""
    out = []
    for name in (live.api_token_env, live.api_token_env + "_NEXT"):
        token = os.environ.get(name, "").strip()
        if name != live.api_token_env and not token:
            continue
        if not valid_token(token):
            print(
                f"chainwatch-live: config error: ${name} must be a random token of at least "
                f"{MIN_TOKEN_CHARS} URL-safe characters (e.g. `openssl rand -hex 32`)",
                file=sys.stderr,
            )
            raise SystemExit(78)  # EX_CONFIG: systemd does not restart on it
        out.append(token)
    return tuple(out)


def edge_token(live: LiveConfig) -> str:
    return edge_tokens(live)[0]


def create_edge_app(live: LiveConfig, token: str | tuple[str, ...]) -> FastAPI:
    tokens = (token,) if isinstance(token, str) else tuple(token)
    if not tokens or not all(valid_token(t) for t in tokens):
        raise ValueError("edge API token too short or not URL-safe")
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

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # the error envelope, without echoing the input back
        return _error(422, "validation_error", "request validation failed", request)

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", None)
        log.error(json.dumps({"msg": "unhandled error", "request_id": rid,
                              "error": type(exc).__name__}))  # fmt: skip
        response = _error(500, "internal_error", "internal server error", request)
        response.headers.update(internal_error_headers(rid))
        return response

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
        app, live, _error, token=tokens, trusted_proxy_hops=live.edge_trusted_proxy_hops
    )
    expected = [f"bearer {t}".encode() for t in tokens]

    def authenticated(scope) -> bool:
        """The service token holder (the dashboard) is not rate limited here, so floods of
        unauthenticated requests from a shared egress IP cannot lock it out."""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                given = value[:7].lower() + value[7:]
                return any([hmac.compare_digest(given, e) for e in expected])
        return False

    app.add_middleware(
        RateLimitMiddleware,
        per_minute=live.edge_rate_limit_per_minute,
        trusted_proxy_hops=live.edge_trusted_proxy_hops,
        exempt=authenticated,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=65_536)
    return app
