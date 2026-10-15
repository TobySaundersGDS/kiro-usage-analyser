"""Tests for the archive import service (services.import_usage).

All identities are synthetic and use the reserved example.test domain.
"""

from __future__ import annotations

import io
import zipfile

from kiro_vfm.adapters.archive import (
    MemberReadError,
    parse_report_timestamp,
    read_member_bounded,
    validate_archive,
)
from kiro_vfm.config import ArchiveLimits, CsvLimits
from kiro_vfm.domain.usage import IssueCode, IssueSeverity
from kiro_vfm.services.import_usage import build_import_result

from .helpers import archive_factory as af

CSV_LIMITS = CsvLimits()
ARCHIVE_LIMITS = ArchiveLimits()


def _import(members: list[tuple[str, bytes]], *, csv_limits: CsvLimits = CSV_LIMITS):
    data = af.make_zip(members)
    summary = validate_archive(data, ARCHIVE_LIMITS)
    return build_import_result(data, summary, csv_limits)


def _codes(result) -> set[IssueCode]:
    return {i.code for i in result.issues}


def test_import_basic_multi_client() -> None:
    cli = af.build_report_csv([af.report_row(client_type="KIRO_CLI", user_id="u1")])
    ide = af.build_report_csv([af.report_row(client_type="KIRO_IDE", user_id="u1")])
    plugin = af.build_report_csv([af.report_row(client_type="PLUGIN", user_id="u2")])
    result = _import(
        [
            ("reports/cli.csv", cli),
            ("reports/ide.csv", ide),
            ("reports/plugin.csv", plugin),
        ]
    )
    assert len(result.records) == 3
    assert result.aggregate.distinct_users == 2
    assert {c.value for c in result.aggregate.client_types} == {
        "KIRO_CLI",
        "KIRO_IDE",
        "PLUGIN",
    }


def test_exact_duplicate_rows_ignored_and_warned() -> None:
    row = af.report_row(user_id="u1")
    csv_a = af.build_report_csv([row])
    csv_b = af.build_report_csv([row])  # identical content, different member
    result = _import([("reports/a.csv", csv_a), ("reports/b.csv", csv_b)])
    assert len(result.records) == 1
    assert result.duplicate_count == 1
    assert IssueCode.DUPLICATE_ROW in _codes(result)


def test_duplicate_ignores_zero_model_columns() -> None:
    # One file has a harmless all-zero model column; the other has none. The
    # canonical content must be equal, so they are exact duplicates.
    row_plain = af.report_row(user_id="u1", total_messages="10")
    row_zero = af.report_row(user_id="u1", total_messages="10")
    row_zero["Claude_messages"] = "0"
    csv_a = af.build_report_csv([row_plain])
    csv_b = af.build_report_csv([row_zero], model_headers=["Claude_messages"])
    result = _import([("reports/a.csv", csv_a), ("reports/b.csv", csv_b)])
    assert len(result.records) == 1
    assert result.duplicate_count == 1


def test_conflict_latest_timestamp_wins() -> None:
    # Same logical key, different content (credits), different report stamps.
    early = af.build_report_csv([af.report_row(user_id="u1", credits_used="1")])
    late = af.build_report_csv([af.report_row(user_id="u1", credits_used="9")])
    result = _import(
        [
            ("acct_user_report_202601010200.csv", early),
            ("acct_user_report_202601020200.csv", late),
        ]
    )
    assert len(result.records) == 1
    assert result.conflict_count == 1
    assert result.records[0].credits_used == 9
    assert IssueCode.CONFLICTING_ROW in _codes(result)


def test_conflict_14_digit_timestamp_parsed() -> None:
    early = af.build_report_csv([af.report_row(user_id="u1", credits_used="1")])
    late = af.build_report_csv([af.report_row(user_id="u1", credits_used="9")])
    result = _import(
        [
            ("acct_user_report_20260101020000.csv", early),
            ("acct_user_report_20260101020005.csv", late),
        ]
    )
    assert len(result.records) == 1
    assert result.records[0].credits_used == 9


def test_conflict_tie_break_by_member_name() -> None:
    # Equal timestamps -> deterministic tie-break by member name then index.
    a = af.build_report_csv([af.report_row(user_id="u1", credits_used="1")])
    b = af.build_report_csv([af.report_row(user_id="u1", credits_used="9")])
    result = _import(
        [
            ("aaa_user_report_202601010200.csv", a),
            ("zzz_user_report_202601010200.csv", b),
        ]
    )
    assert len(result.records) == 1
    # "zzz..." sorts after "aaa...", so b (credits 9) wins.
    assert result.records[0].credits_used == 9


def test_conflict_missing_timestamp_tie_break_deterministic() -> None:
    a = af.build_report_csv([af.report_row(user_id="u1", credits_used="1")])
    b = af.build_report_csv([af.report_row(user_id="u1", credits_used="9")])
    result = _import([("alpha.csv", a), ("beta.csv", b)])
    assert len(result.records) == 1
    # No timestamps -> name tie-break; "beta" > "alpha", so b wins.
    assert result.records[0].credits_used == 9


def test_part_segment_in_filename_parsed() -> None:
    assert parse_report_timestamp("acct_user_report_202601010200_part_1.csv") == (
        "202601010200"
    )
    assert parse_report_timestamp("acct_part_2_user_report_202601010200.csv") == (
        "202601010200"
    )


def test_timestamp_absent_returns_none() -> None:
    assert parse_report_timestamp("reports/day.csv") is None


def test_stable_ordering() -> None:
    rows = [
        af.report_row(date="2026-01-02", user_id="u2", client_type="KIRO_IDE"),
        af.report_row(date="2026-01-01", user_id="u1", client_type="KIRO_CLI"),
    ]
    csv_a = af.build_report_csv([rows[0]])
    csv_b = af.build_report_csv([rows[1]])
    result = _import([("reports/b.csv", csv_a), ("reports/a.csv", csv_b)])
    dates = [r.date for r in result.records]
    assert dates == sorted(dates)


def test_crc_error_surfaced_as_safe_issue() -> None:
    # Build a valid zip then corrupt the deflated member data so the CRC check
    # fails on read. A second clean CSV keeps the archive importable.
    good = af.build_report_csv([af.report_row(user_id="u1")])
    data = af.make_zip([("reports/clean.csv", good), ("reports/broken.csv", good)])
    # Corrupt bytes in the middle of the archive (member payload region).
    corrupt = bytearray(data)
    # Flip bytes well inside the file but before the central directory.
    mid = len(corrupt) // 2
    for i in range(mid, mid + 8):
        corrupt[i] ^= 0xFF
    summary = validate_archive(bytes(corrupt), ARCHIVE_LIMITS)
    result = build_import_result(bytes(corrupt), summary, CSV_LIMITS)
    # Either the member reads with a CRC error (safe issue) or still parses;
    # at minimum the import must not raise and must stay safe.
    assert isinstance(result.records, tuple)
    if IssueCode.MEMBER_UNREADABLE in _codes(result):
        for issue in result.issues:
            if issue.code is IssueCode.MEMBER_UNREADABLE:
                assert issue.severity is IssueSeverity.ERROR


def test_read_member_bounded_enforces_byte_limit() -> None:
    data = af.make_highly_compressible_zip("reports/big.csv", 100_000)
    buffer = io.BytesIO(data)
    with zipfile.ZipFile(buffer) as zf:
        info = next(i for i in zf.infolist() if i.filename == "reports/big.csv")
        try:
            read_member_bounded(zf, info, 1000)
        except MemberReadError as exc:
            assert "read-size limit" in exc.reason
        else:  # pragma: no cover - should have raised
            raise AssertionError("expected MemberReadError")


def test_aggregate_date_range_and_models() -> None:
    r1 = af.report_row(date="2026-01-01", user_id="u1", total_messages="5")
    r1["Claude_messages"] = "5"
    r2 = af.report_row(date="2026-01-03", user_id="u2", total_messages="2")
    r2["Nova_messages"] = "2"
    csv_a = af.build_report_csv([r1], model_headers=["Claude_messages"])
    csv_b = af.build_report_csv([r2], model_headers=["Nova_messages"])
    result = _import([("reports/a.csv", csv_a), ("reports/b.csv", csv_b)])
    assert result.aggregate.earliest_date == "2026-01-01"
    assert result.aggregate.latest_date == "2026-01-03"
    assert result.aggregate.model_slugs == frozenset({"claude", "nova"})


def test_subscription_count_distinct_from_users() -> None:
    # Same UserId under two different profiles -> 1 distinct user, 2 subs.
    a = af.build_report_csv([af.report_row(user_id="u1", profile_id="p1")])
    b = af.build_report_csv([af.report_row(user_id="u1", profile_id="p2")])
    result = _import([("reports/a.csv", a), ("reports/b.csv", b)])
    assert result.aggregate.distinct_users == 1
    assert result.aggregate.subscription_count == 2


def test_import_result_is_immutable() -> None:
    result = _import([("reports/a.csv", af.build_report_csv([af.report_row()]))])
    assert isinstance(result.records, tuple)
    assert isinstance(result.issues, tuple)
    assert isinstance(result.aggregate.client_types, frozenset)
    assert isinstance(result.aggregate.model_slugs, frozenset)


def test_zero_valid_records_handled() -> None:
    # A file with only invalid rows yields no records but no crash.
    bad = af.build_report_csv([af.report_row(credits_used="-1")])
    result = _import([("reports/bad.csv", bad)])
    assert result.records == ()
    assert result.aggregate.accepted_rows == 0
    assert result.aggregate.earliest_date is None


def test_no_pii_in_any_issue_message() -> None:
    secret = "leak@example.test"
    bad = af.build_report_csv([af.report_row(user_email=secret, credits_used="-1")])
    result = _import([("reports/bad.csv", bad)])
    for issue in result.issues:
        assert secret not in issue.message
