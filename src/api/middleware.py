"""Pure-ASGI middleware: security headers, Origin check, body-size limit (spec 6.4, 6.5).

Pure ASGI (not ``BaseHTTPMiddleware``) so server-sent event streams are never buffered.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import urlsplit

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; media-src 'self'; "
    "manifest-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS: dict[bytes, bytes] = {
    b"content-security-policy": CSP.encode(),
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"no-referrer",
    b"permissions-policy": b"camera=(self), microphone=(), geolocation=(), payment=()",
    b"cross-origin-opener-policy": b"same-origin",
    b"cross-origin-resource-policy": b"same-origin",
    b"x-frame-options": b"DENY",
}


async def _json(send: Send, status: int, kind: str, message: str) -> None:
    body = json.dumps({"error": {"kind": kind, "message": message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", b"%d" % len(body)),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeaders:
    """Adds the security headers to every response (a handler's own value wins)."""

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: D102
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for key, value in SECURITY_HEADERS.items():
                    if key not in present:
                        headers.append((key, value))
                if self.hsts and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                if b"cache-control" not in present and scope["path"].startswith("/api"):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


class OriginCheck:
    """Rejects state-changing API requests whose ``Origin`` is not an allowed origin."""

    def __init__(self, app: ASGIApp, *, allowed: list[str]) -> None:
        self.app = app
        self.allowed = {a.rstrip("/").lower() for a in allowed}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: D102
        if (
            scope["type"] == "http"
            and scope["method"] in UNSAFE
            and scope["path"].startswith("/api")
        ):
            headers = dict(scope["headers"])
            origin = headers.get(b"origin", b"").decode("latin-1")
            parts = urlsplit(origin)
            normal = f"{parts.scheme}://{parts.netloc}".lower() if parts.netloc else ""
            if not normal or normal not in self.allowed:
                await _json(send, 403, "permission", "origin not allowed")
                return
        await self.app(scope, receive, send)


class BodyTooLarge(Exception):
    """Raised inside the receive wrapper when a request body exceeds the cap."""


class BodyLimit:
    """Caps request bodies, counting bytes as they stream so an oversized upload is cut early."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: D102
        if scope["type"] != "http" or scope["method"] not in UNSAFE:
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            await _json(send, 413, "limit", "request body too large")
            return
        seen = 0
        started = False

        async def counting() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise BodyTooLarge
            return message

        async def tracking(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting, tracking)
        except BodyTooLarge:
            if not started:
                await _json(send, 413, "limit", "request body too large")
