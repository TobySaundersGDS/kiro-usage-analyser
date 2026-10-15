"""App-level tests: security headers, trusted host, sessions, smoke."""

from __future__ import annotations

import itertools

from fastapi.testclient import TestClient

from kiro_vfm.adapters.archive import ArchiveSummary
from kiro_vfm.app import create_app
from kiro_vfm.config import Settings
from kiro_vfm.services.sessions import SessionStore

from .conftest import build_settings


def test_check_startup_smoke() -> None:
    # Building the app must succeed (acts as --check does).
    app = create_app(build_settings())
    assert app.title == "Kiro VFM analyser"


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_security_headers_present(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "default-src 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["cross-origin-opener-policy"] == "same-origin"
    assert resp.headers["cache-control"] == "no-store"
    # No CORS header should ever be present.
    assert "access-control-allow-origin" not in resp.headers


def test_no_external_assets_in_pages(client: TestClient) -> None:
    body = client.get("/").text
    assert "http://" not in body
    assert "https://" not in body
    assert "cdn" not in body.lower()


def test_trusted_host_rejects_foreign_host() -> None:
    app = create_app(build_settings())
    client = TestClient(app)
    resp = client.get("/", headers={"host": "evil.example.com"})
    assert resp.status_code == 400


def test_trusted_host_allows_localhost() -> None:
    app = create_app(build_settings())
    client = TestClient(app)
    resp = client.get("/", headers={"host": "localhost"})
    assert resp.status_code == 200


def test_csrf_cookie_set_on_get(client: TestClient) -> None:
    resp = client.get("/")
    assert "kiro_vfm_csrf" in resp.cookies
    # Cookie must be HttpOnly and SameSite=strict.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


def test_session_store_expiry() -> None:
    counter = itertools.count()
    times = [0.0, 0.0, 100.0]

    def fake_clock() -> float:
        i = next(counter)
        return times[i] if i < len(times) else 1000.0

    store = SessionStore(ttl_seconds=10, clock=fake_clock)
    summary = ArchiveSummary(
        compressed_bytes=1,
        total_members=1,
        accepted_count=1,
        ignored_count=0,
        rejected_count=0,
        csv_count=1,
        members=(),
    )
    token = store.create(summary)  # clock call 1 -> 0.0
    # clock call 2 -> 0.0 : still fresh
    assert store.get(token) is not None
    # clock call 3 -> 100.0 : expired (>10s inactivity)
    assert store.get(token) is None
    assert len(store) == 0


def test_session_store_unguessable_ids() -> None:
    store = SessionStore(ttl_seconds=10)
    summary = ArchiveSummary(1, 1, 1, 0, 0, 1, ())
    tokens = {store.create(summary) for _ in range(5)}
    assert len(tokens) == 5
    assert all(len(t) >= 32 for t in tokens)


def test_summary_route_unknown_token(client: TestClient) -> None:
    resp = client.get("/summary/does-not-exist")
    assert resp.status_code == 404
    assert "expired or does not exist" in resp.text


def test_oversize_content_length_rejected() -> None:
    settings: Settings = build_settings(max_compressed_bytes=100)
    app = create_app(settings)
    client = TestClient(app)
    # Body must exceed the limit plus the multipart framing allowance (1 MB).
    big = b"x" * (2 * 1024 * 1024)
    resp = client.post(
        "/upload",
        content=big,
        headers={
            "content-type": "application/octet-stream",
            "content-length": str(len(big)),
        },
    )
    assert resp.status_code == 413


def test_413_has_security_headers() -> None:
    # Every body-size 413 must carry the same security headers as any other
    # response (defence in depth; the 413 is produced outside normal routing).
    settings = build_settings(max_compressed_bytes=100)
    app = create_app(settings)
    client = TestClient(app)
    big = b"x" * (2 * 1024 * 1024)
    resp = client.post(
        "/upload",
        content=big,
        headers={
            "content-type": "application/octet-stream",
            "content-length": str(len(big)),
        },
    )
    assert resp.status_code == 413
    assert "default-src 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["cross-origin-opener-policy"] == "same-origin"
    assert resp.headers["cache-control"] == "no-store"


async def _collect_asgi_response(
    app: object, scope: dict, body_chunks: list[bytes]
) -> dict:
    """Drive an ASGI app with a hand-built receive stream and capture output.

    ``body_chunks`` are delivered as ``http.request`` messages that deliberately
    omit any ``Content-Length`` header in ``scope`` and use ``more_body`` to
    simulate a chunked/streamed upload. Returns the response start message plus
    the concatenated body, and records how many chunks were pulled so we can
    assert that the middleware stopped reading early once over limit.
    """

    chunks = list(body_chunks)
    pulled = {"count": 0}

    async def receive() -> dict:
        pulled["count"] += 1
        if chunks:
            chunk = chunks.pop(0)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(chunks),
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    captured: dict = {"status": None, "headers": [], "body": b""}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = message["headers"]
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    await app(scope, receive, send)  # type: ignore[operator]
    captured["chunks_pulled"] = pulled["count"]
    captured["chunks_remaining"] = len(chunks)
    return captured


def _http_scope() -> dict:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/upload",
        "raw_path": b"/upload",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/octet-stream"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }


def test_streamed_body_without_content_length_is_bounded() -> None:
    import asyncio

    from kiro_vfm.app import MaxBodySizeMiddleware

    # A minimal inner ASGI app that drains the entire body, mimicking an
    # endpoint that does `await file.read()`. We wrap it directly in the
    # middleware so we exercise the receive-stream counting in isolation,
    # independent of any downstream per-part parser limits.
    drained = {"bytes": 0, "completed": False}

    async def draining_app(scope, receive, send):
        more = True
        while more:
            message = await receive()
            drained["bytes"] += len(message.get("body", b""))
            more = message.get("more_body", False)
        drained["completed"] = True
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    max_bytes = 100
    mw = MaxBodySizeMiddleware(draining_app, max_bytes=max_bytes)
    limit = max_bytes + MaxBodySizeMiddleware._MULTIPART_OVERHEAD

    # No content-length header; stream chunks that together far exceed the
    # effective limit. The guard must stop before the inner app can drain all
    # of the body.
    chunk = b"x" * (256 * 1024)
    num_chunks = (limit // len(chunk)) + 8
    body_chunks = [chunk] * num_chunks

    # The middleware raises HTTPException out of the receive stream when the
    # inner app (draining_app) has no exception handling of its own.
    import pytest
    from starlette.exceptions import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_collect_asgi_response(mw, _http_scope(), body_chunks))

    assert excinfo.value.status_code == 413
    # The inner app must NOT have completed draining the whole body.
    assert drained["completed"] is False
    # And it must have been stopped before reading everything.
    assert drained["bytes"] <= limit


def test_streamed_body_without_content_length_413_has_headers() -> None:
    # End-to-end: an over-limit streamed multipart upload with NO
    # content-length must surface a 413 carrying the security headers. We use
    # several parts each below Starlette's per-part limit (1 MB) whose running
    # total crosses our effective body-size limit, so our body-size guard (not
    # a per-part guard) is what fires.
    import asyncio

    from kiro_vfm.app import MaxBodySizeMiddleware

    settings = build_settings(max_compressed_bytes=100)
    app = create_app(settings)
    limit = 100 + MaxBodySizeMiddleware._MULTIPART_OVERHEAD  # ~1.05 MB

    boundary = "boundaryXYZ"
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/upload",
        "raw_path": b"/upload",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (
                b"content-type",
                f"multipart/form-data; boundary={boundary}".encode(),
            ),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    def _file_part(name: str, size: int) -> bytes:
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{name}.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        return header + (b"x" * size) + b"\r\n"

    # Each part is 600 KiB (< 1 MiB per-part limit); two of them plus framing
    # exceed the ~1.05 MiB effective limit.
    part_size = 600 * 1024
    body_chunks = [
        _file_part("file", part_size),
        _file_part("extra", part_size),
        f"--{boundary}--\r\n".encode(),
    ]
    # Sanity: a single part is under Starlette's per-part cap, the total is not
    # under our effective limit.
    assert part_size < 1024 * 1024
    assert 2 * part_size > limit

    result = asyncio.run(_collect_asgi_response(app, scope, body_chunks))

    assert result["status"] == 413
    header_map = {k.decode().lower(): v.decode() for k, v in result["headers"]}
    assert "default-src 'none'" in header_map["content-security-policy"]
    assert header_map["x-content-type-options"] == "nosniff"


def test_streamed_body_within_limit_passes_through() -> None:
    import asyncio

    from kiro_vfm.app import MaxBodySizeMiddleware

    reached = {"ok": False}

    async def inner_app(scope, receive, send):
        more = True
        while more:
            message = await receive()
            more = message.get("more_body", False)
        reached["ok"] = True
        await send(
            {"type": "http.response.start", "status": 204, "headers": []}
        )
        await send({"type": "http.response.body", "body": b""})

    mw = MaxBodySizeMiddleware(inner_app, max_bytes=100)

    # A small streamed body with no content-length must NOT trip the guard.
    result = asyncio.run(
        _collect_asgi_response(mw, _http_scope(), [b"small body"])
    )

    assert result["status"] == 204
    assert reached["ok"] is True
