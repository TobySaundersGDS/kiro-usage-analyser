"""Route tests for the Level 1 recorded-usage analysis page.

All identities are synthetic and use the reserved example.test domain.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from kiro_vfm.app import create_app

from .conftest import build_settings
from .helpers import archive_factory as af

GOOD_ORIGIN = "http://127.0.0.1:8000"


def _upload_and_token(client: TestClient, zip_bytes: bytes) -> str:
    client.get("/")
    csrf = client.cookies["kiro_vfm_csrf"]
    resp = client.post(
        "/upload",
        files={"file": ("r.zip", zip_bytes, "application/zip")},
        data={"csrf_token": csrf},
        headers={"origin": GOOD_ORIGIN},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return resp.headers["location"].rsplit("/", 1)[-1]


def _two_day_two_client_archive() -> bytes:
    r1 = af.report_row(
        date="2026-01-01",
        user_id="u1",
        client_type="KIRO_CLI",
        credits_used="1.5",
        total_messages="10",
        user_email="alice@example.test",
    )
    r1["Claude_messages"] = "10"
    r2 = af.report_row(
        date="2026-01-02",
        user_id="u2",
        client_type="KIRO_IDE",
        credits_used="0.25",
        total_messages="5",
        user_email="bob@example.test",
    )
    r2["Nova_messages"] = "5"
    csv_a = af.build_report_csv([r1], model_headers=["Claude_messages"])
    csv_b = af.build_report_csv([r2], model_headers=["Nova_messages"])
    return af.make_zip(
        [
            ("acct_user_report_202601010200.csv", csv_a),
            ("acct_user_report_202601020200.csv", csv_b),
        ]
    )


def test_analysis_page_shows_exact_totals() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    resp = client.get(f"/analysis/{token}")
    assert resp.status_code == 200
    assert "Recorded usage" in resp.text
    # Exact decimal total 1.5 + 0.25 == 1.75 (no float drift, no rounding).
    assert "1.75" in resp.text


def test_analysis_page_shows_person_email() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    resp = client.get(f"/analysis/{token}")
    # Level 1 person view is allowed to display emails for reconciliation.
    assert "alice@example.test" in resp.text
    assert "bob@example.test" in resp.text


def test_analysis_page_shows_model_breakdown() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    resp = client.get(f"/analysis/{token}")
    assert "claude" in resp.text
    assert "nova" in resp.text


def test_analysis_filter_by_client() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    resp = client.get(f"/analysis/{token}?client=KIRO_CLI")
    assert resp.status_code == 200
    # Only the CLI record (alice) should contribute; bob is filtered out.
    assert "alice@example.test" in resp.text
    assert "bob@example.test" not in resp.text


def test_analysis_filter_by_date_range() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    resp = client.get(f"/analysis/{token}?date_from=2026-01-02")
    assert "bob@example.test" in resp.text
    assert "alice@example.test" not in resp.text


def test_analysis_invalid_filter_ignored() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    # Bogus filter values must degrade to showing everything, not error.
    resp = client.get(f"/analysis/{token}?date_from=bad&client=NOPE")
    assert resp.status_code == 200
    assert "alice@example.test" in resp.text
    assert "bob@example.test" in resp.text


def test_analysis_empty_filter_result() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    resp = client.get(f"/analysis/{token}?date_from=2030-01-01")
    assert resp.status_code == 200
    assert "No recorded usage matches" in resp.text


def test_analysis_unknown_token_404() -> None:
    client = TestClient(create_app(build_settings()))
    resp = client.get("/analysis/does-not-exist")
    assert resp.status_code == 404
    assert "expired or does not exist" in resp.text


def test_analysis_escapes_email_metacharacters() -> None:
    client = TestClient(create_app(build_settings()))
    # A synthetic email containing HTML metacharacters must be escaped.
    row = af.report_row(user_email="<script>x</script>@example.test")
    csv_bytes = af.build_report_csv([row])
    data = af.make_zip([("reports/day.csv", csv_bytes)])
    token = _upload_and_token(client, data)
    resp = client.get(f"/analysis/{token}")
    assert "<script>x</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_analysis_contains_message_distinction_note() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    resp = client.get(f"/analysis/{token}")
    assert "prompts, responses, and tool calls" in resp.text
    assert "not" in resp.text  # the disclaimer negates API-invocation equivalence


def test_analysis_has_no_pricing_or_estimate_values() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    resp = client.get(f"/analysis/{token}")
    text = resp.text
    # No USD figures, no estimate/value-for-money wording, no coverage %.
    assert "USD" not in text
    assert "value for money" not in text.lower()
    assert "estimated cost" not in text.lower()
    assert "$" not in text  # no dollar amounts anywhere on the recorded page


def test_summary_links_to_analysis() -> None:
    client = TestClient(create_app(build_settings()))
    token = _upload_and_token(client, _two_day_two_client_archive())
    resp = client.get(f"/summary/{token}")
    assert f"/analysis/{token}" in resp.text
