"""End-to-end upload route tests via the FastAPI TestClient."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from kiro_vfm.app import create_app

from .conftest import build_settings
from .helpers import archive_factory as af

GOOD_ORIGIN = "http://127.0.0.1:8000"


def _get_csrf(client: TestClient) -> str:
    client.get("/")
    return client.cookies["kiro_vfm_csrf"]


def _upload(
    client: TestClient,
    zip_bytes: bytes,
    *,
    csrf: str | None = None,
    origin: str | None = GOOD_ORIGIN,
    filename: str = "reports.zip",
    follow: bool = False,
):
    if csrf is None:
        csrf = _get_csrf(client)
    headers = {}
    if origin is not None:
        headers["origin"] = origin
    return client.post(
        "/upload",
        files={"file": (filename, zip_bytes, "application/zip")},
        data={"csrf_token": csrf},
        headers=headers,
        follow_redirects=follow,
    )


def test_valid_upload_redirects_to_summary() -> None:
    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("reports/day.csv", b"Date,Credits_Used\n2026-01-01,1\n")])
    resp = _upload(client, data)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/summary/")


def test_valid_upload_summary_page() -> None:
    client = TestClient(create_app(build_settings()))
    data = af.make_zip(
        [
            ("reports/a.csv", b"col\n1\n"),
            ("reports/b.csv", b"col\n2\n"),
            ("__MACOSX/._a.csv", b"junk"),
        ]
    )
    resp = _upload(client, data, follow=True)
    assert resp.status_code == 200
    assert "Archive validated" in resp.text
    assert "Accepted CSV reports" in resp.text


def test_upload_rejects_missing_csrf() -> None:
    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    # Obtain a cookie but submit an empty token field.
    client.get("/")
    resp = client.post(
        "/upload",
        files={"file": ("r.zip", data, "application/zip")},
        data={"csrf_token": ""},
        headers={"origin": GOOD_ORIGIN},
    )
    assert resp.status_code == 403
    assert "CSRF" in resp.text


def test_upload_rejects_mismatched_csrf() -> None:
    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    client.get("/")
    resp = client.post(
        "/upload",
        files={"file": ("r.zip", data, "application/zip")},
        data={"csrf_token": "not-the-cookie-value"},
        headers={"origin": GOOD_ORIGIN},
    )
    assert resp.status_code == 403


def test_upload_rejects_foreign_origin() -> None:
    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    resp = _upload(client, data, origin="http://evil.example.com")
    assert resp.status_code == 403
    assert "origin could not be verified" in resp.text


def test_upload_allows_missing_origin_with_valid_csrf() -> None:
    """Browsers/privacy tools may omit both Origin and Referer on HTML forms."""

    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    resp = _upload(client, data, origin=None)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/summary/")


def test_referrer_policy_is_same_origin_not_no_referrer() -> None:
    """Regression: ``no-referrer`` makes Chrome send ``Origin: null`` on a
    same-origin form POST, which the origin check rejects and breaks real
    uploads. ``same-origin`` lets the browser send a proper Origin for our own
    POST while still stripping the referrer cross-origin.
    """

    client = TestClient(create_app(build_settings()))
    resp = client.get("/")
    assert resp.headers["referrer-policy"] == "same-origin"


def test_upload_accepts_loopback_origin_matching_host() -> None:
    """A real browser on 127.0.0.1:8000 sends this exact Origin; it must pass."""

    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("reports/day.csv", b"Date,Credits_Used\n2026-01-01,1\n")])
    resp = _upload(client, data, origin="http://127.0.0.1:8000")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/summary/")


def test_upload_without_origin_still_requires_valid_csrf() -> None:
    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    client.get("/")
    resp = client.post(
        "/upload",
        files={"file": ("r.zip", data, "application/zip")},
        data={"csrf_token": "not-the-cookie-value"},
    )
    assert resp.status_code == 403
    assert "CSRF" in resp.text


def test_upload_rejects_opaque_origin() -> None:
    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    resp = _upload(client, data, origin="null")
    assert resp.status_code == 403
    assert "origin could not be verified" in resp.text


def test_upload_accepts_trusted_referer_when_origin_is_absent() -> None:
    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    csrf = _get_csrf(client)
    resp = client.post(
        "/upload",
        files={"file": ("r.zip", data, "application/zip")},
        data={"csrf_token": csrf},
        headers={"referer": "http://localhost:8000/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_upload_rejects_non_zip() -> None:
    client = TestClient(create_app(build_settings()))
    resp = _upload(client, b"not a zip at all")
    assert resp.status_code == 400
    assert "not a valid ZIP" in resp.text


def test_upload_rejects_no_csv() -> None:
    client = TestClient(create_app(build_settings()))
    data = af.make_zip([("notes.txt", b"hello")])
    resp = _upload(client, data)
    assert resp.status_code == 400
    assert "no CSV reports" in resp.text


def test_exactly_at_compressed_limit_accepted() -> None:
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    settings = build_settings(max_compressed_bytes=len(data))
    client = TestClient(create_app(settings))
    resp = _upload(client, data)
    assert resp.status_code == 303


def test_over_compressed_limit_rejected_by_validator() -> None:
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    # Set limit one byte below the archive size; Content-Length guard or the
    # validator must reject it. The TestClient sends Content-Length for the
    # multipart body (larger than the zip), so the 413 guard fires first.
    settings = build_settings(max_compressed_bytes=len(data) - 1)
    client = TestClient(create_app(settings))
    resp = _upload(client, data)
    assert resp.status_code in (400, 413)


def test_summary_escapes_member_names() -> None:
    client = TestClient(create_app(build_settings()))
    # A rejected member with HTML metacharacters in its (safe) name portion.
    data = af.make_zip(
        [
            ("reports/day.csv", b"col\n1\n"),
            ("reports/<script>evil</script>.txt", b"x"),
        ]
    )
    resp = _upload(client, data, follow=True)
    assert resp.status_code == 200
    # The raw script tag must not appear; it must be HTML-escaped.
    assert "<script>evil</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_no_sensitive_logging(caplog) -> None:
    client = TestClient(create_app(build_settings()))
    secret_email = "person@example.test"
    csv = f"Date,User_Email\n2026-01-01,{secret_email}\n".encode()
    data = af.make_zip([("reports/day.csv", csv)])
    with caplog.at_level(logging.DEBUG):
        resp = _upload(client, data, follow=True)
    assert resp.status_code == 200
    # No upload content / PII must appear in logs.
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_email not in joined
    assert "person@example" not in joined


def test_session_not_persisted_across_app_instances() -> None:
    settings = build_settings()
    client1 = TestClient(create_app(settings))
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    resp = _upload(client1, data)
    location = resp.headers["location"]

    # A fresh app instance (simulating restart) has no knowledge of the token.
    client2 = TestClient(create_app(settings))
    resp2 = client2.get(location)
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# Task 2: parsed import summary integration.
# ---------------------------------------------------------------------------


def test_summary_shows_aggregate_import_metadata() -> None:
    client = TestClient(create_app(build_settings()))
    row = af.report_row(user_id="u1", total_messages="4")
    row["Claude_messages"] = "4"
    csv_bytes = af.build_report_csv([row], model_headers=["Claude_messages"])
    data = af.make_zip([("acct_user_report_202601010200.csv", csv_bytes)])
    resp = _upload(client, data, follow=True)
    assert resp.status_code == 200
    assert "Accepted CSV rows" in resp.text
    assert "Observed subscriptions" in resp.text
    assert "Data quality issues" in resp.text
    assert "claude" in resp.text  # canonical model slug shown
    assert "2026-01-01" in resp.text  # date range shown


def test_summary_does_not_leak_email_or_raw_rows() -> None:
    client = TestClient(create_app(build_settings()))
    secret_email = "private@example.test"
    secret_user = "user-secret-id"
    row = af.report_row(user_email=secret_email, user_id=secret_user)
    csv_bytes = af.build_report_csv([row])
    data = af.make_zip([("reports/day.csv", csv_bytes)])
    resp = _upload(client, data, follow=True)
    assert resp.status_code == 200
    assert secret_email not in resp.text
    assert secret_user not in resp.text


def test_summary_issue_table_escapes_source_names() -> None:
    client = TestClient(create_app(build_settings()))
    # A member whose name has HTML metacharacters, carrying an invalid row so
    # it appears in the issue table with a safe, escaped source name.
    row = af.report_row(credits_used="-1")
    csv_bytes = af.build_report_csv([row])
    data = af.make_zip([("reports/<b>x</b>.csv", csv_bytes)])
    resp = _upload(client, data, follow=True)
    assert resp.status_code == 200
    assert "<b>x</b>" not in resp.text
    assert "&lt;b&gt;" in resp.text


def test_summary_handles_zero_valid_records() -> None:
    client = TestClient(create_app(build_settings()))
    bad = af.build_report_csv([af.report_row(credits_used="-1")])
    data = af.make_zip([("reports/bad.csv", bad)])
    resp = _upload(client, data, follow=True)
    assert resp.status_code == 200
    assert "No valid usage records" in resp.text


def test_session_stores_import_result_not_bytes() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)
    data = af.make_zip([("reports/day.csv", af.build_report_csv([af.report_row()]))])
    resp = _upload(client, data)
    token = resp.headers["location"].rsplit("/", 1)[-1]
    stored = app.state.sessions.get(token)
    # The stored value is the immutable ImportResult, never raw bytes.
    from kiro_vfm.domain.usage import ImportResult

    assert isinstance(stored, ImportResult)
    assert isinstance(stored.records, tuple)
    # Defensive: no attribute holds raw archive/CSV bytes.
    assert not any(
        isinstance(getattr(stored, name, None), bytes | bytearray)
        for name in ("records", "issues", "archive", "aggregate")
    )


def test_duplicate_and_conflict_counts_reported_in_summary() -> None:
    client = TestClient(create_app(build_settings()))
    row = af.report_row(user_id="u1", credits_used="1")
    dup = af.build_report_csv([row])
    conflict = af.build_report_csv([af.report_row(user_id="u1", credits_used="5")])
    data = af.make_zip(
        [
            ("acct_user_report_202601010200.csv", dup),
            ("acct_user_report_202601010200_dup.csv", af.build_report_csv([row])),
            ("acct_user_report_202601020200.csv", conflict),
        ]
    )
    resp = _upload(client, data, follow=True)
    assert resp.status_code == 200
    assert "Exact-duplicate rows ignored" in resp.text
    assert "Conflicting rows discarded" in resp.text


def test_invalid_utf8_member_reported_but_import_succeeds() -> None:
    client = TestClient(create_app(build_settings()))
    good = af.build_report_csv([af.report_row(user_id="u1")])
    data = af.make_zip(
        [
            ("reports/good.csv", good),
            ("reports/bad.csv", b"\xff\xfe not utf-8"),
        ]
    )
    resp = _upload(client, data, follow=True)
    assert resp.status_code == 200
    assert "invalid_encoding" in resp.text
