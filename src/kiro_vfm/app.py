"""FastAPI application factory for the Kiro VFM analyser."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings
from .domain.usage import ImportResult
from .services.sessions import SessionStore
from .web.routes import router
from .web.security import SecurityHeadersMiddleware, apply_security_headers

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class MaxBodySizeMiddleware:
    """Bound the total request body, counting the actual ASGI receive stream.

    This does not trust ``Content-Length``. The limit is enforced by counting
    the bytes of every ``http.request`` message as it is pulled from the ASGI
    ``receive`` channel, so an over-limit body is stopped even when
    ``Content-Length`` is absent, lies, or the transfer is chunked. Because the
    guard sits in the receive path, multipart parsing (and therefore any
    downstream ``await file.read()``) can never consume more than the bound.

    A fixed multipart-framing allowance is added on top of the compressed-size
    limit so legitimate ``multipart/form-data`` boundaries and part headers are
    not counted against the archive-size budget; the archive validator still
    enforces the exact limit on the real archive bytes as defence in depth.

    When the limit is exceeded mid-body we raise :class:`HTTPException` (413)
    from the wrapped ``receive`` callable. FastAPI deliberately re-raises
    ``HTTPException`` out of its form/body parsing, so the error reaches the
    exception middleware and is rendered as a 413 that flows back out through
    :class:`SecurityHeadersMiddleware`, picking up the standard security
    headers. We stop pulling further chunks from the client at that point. An
    over-limit ``Content-Length`` is rejected up front with the same 413 and
    the same headers applied directly.
    """

    # Generous allowance for multipart/form-data framing (boundaries, headers).
    _MULTIPART_OVERHEAD = 1 * 1024 * 1024

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    @property
    def _effective_limit(self) -> int:
        return self.max_bytes + self._MULTIPART_OVERHEAD

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self._effective_limit

        # Fast path: reject an over-limit declared Content-Length up front.
        headers = dict(scope.get("headers") or [])
        raw_len = headers.get(b"content-length")
        if raw_len is not None:
            try:
                declared = int(raw_len)
            except ValueError:
                declared = -1
            if declared > limit:
                await self._send_413(scope, receive, send)
                return

        total = 0

        async def receive_with_limit() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    # Stop here: do not pull further chunks from the client.
                    # HTTPException is re-raised by FastAPI's body parser and
                    # rendered as a 413 by the exception middleware, so the
                    # response still gains security headers on the way out.
                    raise HTTPException(status_code=413, detail="Payload too large")
            return message

        await self.app(scope, receive_with_limit, send)

    async def _send_413(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = PlainTextResponse("Payload too large", status_code=413)
        apply_security_headers(response)
        await response(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    app = FastAPI(title="Kiro VFM analyser", docs_url=None, redoc_url=None, openapi_url=None)

    # Shared state (ephemeral; no DB).
    app.state.settings = settings
    app.state.sessions = SessionStore[ImportResult](ttl_seconds=settings.session_ttl_seconds)
    # Hosts we consider "ourselves" for origin verification.
    app.state.allowed_hosts = frozenset({settings.host, "localhost", "127.0.0.1", "testserver"})

    # Middleware is applied outermost-first in the order of the final wrapped
    # stack. We want SecurityHeadersMiddleware to be the *outermost* layer so
    # it decorates every response — including the 413 that the body-size guard
    # produces via an HTTPException rendered by the inner exception middleware.
    # add_middleware prepends, so the LAST call becomes the outermost layer.
    #
    # Resulting order (outermost -> innermost):
    #   SecurityHeadersMiddleware -> MaxBodySizeMiddleware
    #     -> TrustedHostMiddleware -> routing
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", settings.host, "testserver"],
    )
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.limits.max_compressed_bytes)
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app
