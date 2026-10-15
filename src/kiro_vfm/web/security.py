"""Security helpers for the local application.

Includes a restrictive security-header middleware and CSRF/origin/host checks
appropriate for a localhost-only tool.

CSRF uses the *stateless raw double-submit* pattern: a high-entropy,
HttpOnly token is set as a cookie and the same value must be echoed back in a
form field. The two values are compared with a constant-time check. The token
is **not** signed; for a localhost-only tool with no shared secret and no
server-side session store, a sufficiently long random token (256 bits of
entropy) is unguessable on its own, so signing would add key-management
complexity without changing the security properties. As defence in depth,
any ``Origin`` or ``Referer`` supplied by the browser must name one of the
application's trusted local hosts. Some browsers and privacy settings omit
both headers for ordinary HTML form submissions; in that case the valid
CSRF cookie/form-token pair remains mandatory.
"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_COOKIE_NAME = "kiro_vfm_csrf"
CSRF_FIELD_NAME = "csrf_token"

# No external assets: everything is same-origin. 'self' only, no CDN, no CORS.
_CSP = (
    "default-src 'none'; "
    "style-src 'self'; "
    "img-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # ``same-origin`` (not ``no-referrer``): a referrer policy that strips the
    # referrer entirely causes some browsers (e.g. Chrome) to serialise a
    # same-origin form submission's Origin as the opaque value ``null``, which
    # our origin check correctly rejects — breaking legitimate uploads. With
    # ``same-origin`` the browser still sends no referrer cross-origin (privacy
    # preserved) but sends a proper Origin/Referer for our own same-origin
    # POST, so origin verification works without having to trust ``null``.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cache-Control": "no-store",
}


class SecurityHeadersMiddleware:
    """Attach the standard security headers to every HTTP response.

    Implemented as a pure-ASGI middleware (rather than
    ``BaseHTTPMiddleware``) so that it reliably decorates *every* response —
    including error responses such as the body-size ``413`` and anything
    produced by inner exception handling — without buffering the request body
    or interfering with exceptions raised while the body is being read.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    if key not in headers:
                        headers[key] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def apply_security_headers(response: Response) -> None:
    """Apply the standard security headers to ``response`` in place.

    Retained for responses constructed directly (for example a fast-path
    ``413`` for an over-limit ``Content-Length``) so they carry exactly the
    same header set even if they never pass back through the middleware.
    """

    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)


def new_csrf_token() -> str:
    """Return a fresh high-entropy CSRF token (256 bits, URL-safe).

    The token is random and unsigned. It is stored verbatim in an HttpOnly
    cookie and echoed back in a form field (raw double-submit).
    """

    return secrets.token_urlsafe(32)


def tokens_match(a: str | None, b: str | None) -> bool:
    """Constant-time equality check for the double-submit CSRF tokens."""

    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


def origin_is_allowed(request: Request, allowed_hosts: frozenset[str]) -> bool:
    """Validate browser-supplied origin metadata when it is available.

    ``Origin`` is preferred, with ``Referer`` as a fallback. Browsers and
    privacy tools are permitted to omit both for a normal HTML form POST; the
    route still requires the unguessable double-submit CSRF token in that
    case. A supplied opaque, malformed, or non-local origin is rejected.
    """

    candidate = request.headers.get("origin") or request.headers.get("referer")
    if not candidate:
        return True
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    host = parsed.hostname
    if host is None:
        return False
    return host in allowed_hosts


__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_FIELD_NAME",
    "SECURITY_HEADERS",
    "SecurityHeadersMiddleware",
    "apply_security_headers",
    "new_csrf_token",
    "origin_is_allowed",
    "tokens_match",
]
