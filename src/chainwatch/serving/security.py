"""Hardening for public exposure: body limits, rate limiting, headers, request IDs.

The service is read-only and holds no user data. These controls keep a public demo
from being knocked over by oversized or high-rate requests; they are not a substitute
for a reverse proxy in a real deployment.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Dashboard CSP: everything same-origin; recharts/react use inline style attributes.
DASHBOARD_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'"
)
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    # ignored by browsers over plain http (local use); enforced behind Render's/Caddy's TLS
    "strict-transport-security": "max-age=31536000",
}


def client_key(scope: Scope, trusted_proxy_hops: int = 0) -> str:
    """Client identity for rate limits and per-client caps.

    ``trusted_proxy_hops`` is the number of reverse proxies in front of the app that
    *append* to X-Forwarded-For; the key is the entry the outermost trusted proxy appended
    (counted from the right), so a client-supplied header value cannot change it. With 0
    the header is ignored and the socket peer is used. IPv6 clients are keyed by their /64,
    the smallest block a single subscriber usually controls (rotating addresses inside it
    must not reset the limit).
    """
    addr = None
    if trusted_proxy_hops:
        # repeated headers are equivalent to one comma-joined header (RFC 9110)
        entries = [
            part.strip()
            for name, value in scope.get("headers", [])
            if name == b"x-forwarded-for"
            for part in value.decode("latin-1").split(",")
        ]
        entries = [e for e in entries if e]
        if len(entries) >= trusted_proxy_hops:
            addr = entries[-trusted_proxy_hops]
    if addr is None:
        client = scope.get("client")
        addr = client[0] if client else "unknown"
    try:
        ip = ipaddress.ip_address(addr.strip("[]"))
    except ValueError:
        return addr[:64]
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def request_id(header_value: str | None) -> str:
    """Accept a client request ID only if it is short and plain; otherwise generate one."""
    if header_value and REQUEST_ID_RE.match(header_value):
        return header_value
    return uuid.uuid4().hex[:16]


def _json_error(status: int, code: str, message: str, extra_headers=()) -> tuple[dict, bytes]:
    body = json.dumps(
        {"error": {"code": code, "message": message, "details": None}, "request_id": None}
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        *((k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()),
        *extra_headers,
    ]
    return {"type": "http.response.start", "status": status, "headers": headers}, body


class BodySizeLimitMiddleware:
    """Reject request bodies above ``max_bytes`` *before* they are parsed.

    Checks Content-Length up front and also counts streamed (chunked) bodies, so a large
    JSON document never reaches the JSON parser / pydantic on the event loop.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                await self._reject(send)
                return
        seen = 0
        sent_response = False

        async def limited_receive() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge
            return msg

        async def tracking_send(msg: Message) -> None:
            nonlocal sent_response
            if msg["type"] == "http.response.start":
                sent_response = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not sent_response:
                await self._reject(send)

    async def _reject(self, send: Send) -> None:
        start, body = _json_error(
            413, "payload_too_large", f"request body exceeds {self.max_bytes} bytes"
        )
        await send(start)
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass


class RateLimitMiddleware:
    """Token bucket per client (see ``client_key``) for /v1 routes; health checks and
    requests for which ``exempt(scope)`` is true (e.g. a valid service token) are not
    limited, so unauthenticated traffic cannot drain an authenticated caller's budget."""

    def __init__(
        self,
        app: ASGIApp,
        per_minute: int,
        burst: int | None = None,
        trusted_proxy_hops: int = 0,
        max_clients: int = 10_000,
        exempt: Callable[[Scope], bool] | None = None,
    ) -> None:
        self.app = app
        self.rate = per_minute / 60.0
        self.burst = float(burst or max(1, per_minute // 4))
        self.hops = max(0, int(trusted_proxy_hops))
        self.max_clients = max_clients
        self.buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self.lock = threading.Lock()
        self.exempt = exempt

    def _client(self, scope: Scope) -> str:
        return client_key(scope, self.hops)

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        now = time.monotonic() if now is None else now
        with self.lock:
            tokens, last = self.buckets.pop(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            ok = tokens >= 1.0
            if ok:
                tokens -= 1.0
            self.buckets[key] = (tokens, now)
            while len(self.buckets) > self.max_clients:  # bounded memory
                self.buckets.popitem(last=False)
            retry = 0.0 if ok else (1.0 - tokens) / self.rate
            return ok, retry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("path", "").startswith("/v1")
            and not (self.exempt and self.exempt(scope))
        ):
            ok, retry = self.allow(self._client(scope))
            if not ok:
                start, body = _json_error(
                    429,
                    "rate_limited",
                    "too many requests; slow down",
                    [(b"retry-after", str(max(1, round(retry))).encode())],
                )
                await send(start)
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
