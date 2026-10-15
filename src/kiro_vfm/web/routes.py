"""HTTP routes for upload and import-summary pages."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi import File as FormFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..adapters.archive import ArchiveError, validate_archive
from ..services.import_usage import build_import_result
from ..services.recorded_analysis import analyse
from .security import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    new_csrf_token,
    origin_is_allowed,
    tokens_match,
)
from .view_models import build_filter_view, format_decimal, parse_filter

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# Expose exact Decimal formatting to templates (no rounding, no float).
templates.env.filters["decimal"] = format_decimal

router = APIRouter()


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        secure=False,  # localhost over http
        path="/",
    )


def _render_upload(request: Request, *, error: str | None, status: int) -> HTMLResponse:
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    csrf_token = existing or new_csrf_token()
    body = templates.get_template("upload.html").render(
        request=request, csrf_token=csrf_token, error=error
    )
    response = HTMLResponse(body, status_code=status)
    if not existing:
        _set_csrf_cookie(response, csrf_token)
    return response


@router.get("/", response_class=HTMLResponse)
async def upload_page(request: Request) -> Response:
    return _render_upload(request, error=None, status=200)


@router.post("/upload")
async def handle_upload(
    request: Request,
    file: UploadFile = FormFile(...),
    csrf_token: str = Form(default=""),
) -> Response:
    settings = request.app.state.settings
    sessions = request.app.state.sessions
    allowed_hosts: frozenset[str] = request.app.state.allowed_hosts

    # Origin / Referer check for this unsafe method.
    if not origin_is_allowed(request, allowed_hosts):
        return _render_upload(
            request, error="Request origin could not be verified.", status=403
        )

    # Double-submit CSRF check.
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not tokens_match(cookie_token, csrf_token):
        return _render_upload(request, error="Invalid or missing CSRF token.", status=403)

    # Read the upload fully into memory (bounded) then discard the spool file.
    try:
        data = await file.read()
    finally:
        await file.close()

    try:
        summary = validate_archive(data, settings.limits)
    except ArchiveError as exc:
        del data  # drop uploaded bytes immediately on failure
        return _render_upload(request, error=f"Upload rejected: {exc.reason}", status=400)

    # Parse the accepted CSV members into an immutable, PII-safe-by-aggregate
    # ImportResult *before* we drop the uploaded bytes. Members are read one at
    # a time with a hard byte bound; no bytes/streams/rows are retained.
    import_result = build_import_result(data, summary, settings.csv_limits)

    del data  # only the immutable ImportResult is retained in memory

    token = sessions.create(import_result)
    return RedirectResponse(url=f"/summary/{token}", status_code=303)


@router.get("/summary/{token}", response_class=HTMLResponse)
async def summary_page(request: Request, token: str) -> Response:
    sessions = request.app.state.sessions
    result = sessions.get(token)
    if result is None:
        return _render_upload(
            request,
            error="This analysis session has expired or does not exist.",
            status=404,
        )
    body = templates.get_template("import_summary.html").render(
        request=request, result=result, token=token
    )
    return HTMLResponse(body)


@router.get("/analysis/{token}", response_class=HTMLResponse)
async def analysis_page(
    request: Request,
    token: str,
    date_from: str | None = None,
    date_to: str | None = None,
    client: str | None = None,
) -> Response:
    """Level 1 recorded-usage analysis for a stored import.

    Shows exact recorded facts only — no estimates, pricing, token counts, or
    value-for-money calculations. Filterable by date range and client type.
    """

    sessions = request.app.state.sessions
    result = sessions.get(token)
    if result is None:
        return _render_upload(
            request,
            error="This analysis session has expired or does not exist.",
            status=404,
        )

    usage_filter = parse_filter(date_from=date_from, date_to=date_to, client=client)
    analysis = analyse(result, usage_filter)
    filter_view = build_filter_view(analysis)

    body = templates.get_template("recorded_usage.html").render(
        request=request,
        token=token,
        analysis=analysis,
        filter_view=filter_view,
    )
    return HTMLResponse(body)


@router.get("/healthz", response_class=HTMLResponse)
async def healthz() -> Response:
    return HTMLResponse("ok")


__all__ = ["router", "CSRF_FIELD_NAME"]
