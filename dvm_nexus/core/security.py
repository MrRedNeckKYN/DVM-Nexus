"""
DVM Nexus - Same-origin protection (anti-CSRF / anti cross-site WebSocket hijacking).

This is NOT authentication. It simply refuses state-changing requests (POST/PUT/PATCH/DELETE)
and WebSocket upgrades that a *browser* reports as originating from a different site than the
one it is talking to, so a random web page open in the operator's browser cannot silently
drive the Pi (restart the radio, reboot, use the terminal, ...).

Requests without an Origin header (curl, scripts, SSH-tunnelled tools, the test client) are
unaffected, as are normal in-app requests (same Origin as Host).

Reverse-proxy / alternate-hostname setups can whitelist extra hosts with
``NEXUS_ALLOWED_ORIGINS="nexus.local,pi.example:8443"``.
"""

import os
from typing import Dict, Set
from urllib.parse import urlparse

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _extra_allowed_hosts() -> Set[str]:
    raw = os.getenv("NEXUS_ALLOWED_ORIGINS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def origin_is_allowed(origin: str, host: str) -> bool:
    """Return True if a browser-supplied Origin header is same-origin with Host (or whitelisted)."""
    if not origin:
        return True
    origin_host = urlparse(origin).netloc.lower()
    if not origin_host:  # "null" origins (sandboxed iframes, file://) are never trusted
        return False
    return origin_host == (host or "").lower() or origin_host in _extra_allowed_hosts()


class OriginCheckMiddleware:
    """Pure-ASGI middleware (also covers WebSocket scopes, which BaseHTTPMiddleware skips)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope_type = scope["type"]
        if scope_type not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope_type == "http" and scope.get("method", "GET").upper() not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers: Dict[str, str] = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        if origin_is_allowed(headers.get("origin", ""), headers.get("host", "")):
            await self.app(scope, receive, send)
            return

        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        body = b"Cross-origin request blocked by DVM Nexus (Origin does not match Host)."
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
