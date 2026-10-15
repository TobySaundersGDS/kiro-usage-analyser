"""Unit tests for the Kiro CSV parser (adapters.kiro_csv).

All identities are synthetic and use the reserved example.test domain.
"""

from __future__ import annotations

from decimal import Decimal

from kiro_vfm.adapters.kiro_csv import parse_member_csv
from kiro_vfm.config import CsvLimits
from kiro_vfm.domain.usage import ClientType, IssueCode, IssueSeverity, SourceRef

from .helpers import archive_factory as af

LIMITS = CsvLimits()
BIG = 1_000_000


def _src(member: str = "reports/day.csv", index: int = 0, ts: str | None = None) -> SourceRef:
    return SourceRef(member=member, index=index, report_timestamp=ts)


def _parse(data: bytes, *, limits: CsvLimits = LIMITS, remaining: int = BIG):
    return parse_member_csv(data, _src(), limits, remaining_import_rows=remaining)


def _codes(issues) -> set[IssueCode]:
    return {i.code for i in issues}


def test_parses_basic_cli_row() -> None:
    data = af.build_report_csv(
        [af.report_row()],
        model_headers=["Claude_messages"],
    )
    # model column value defaults to "0"; set a real count via a custom row.
    row = af.report_row(total_messages="10")
    row["Claude_messages"] = "10"
    data = af.build_report_csv([row], model_headers=["Claude_messages"])
    out = _parse(data)
    assert out.records
    rec = out.records[0]
    assert rec.client_type is ClientType.KIRO_CLI
    assert rec.credits_used == Decimal("1.5")
    assert rec.models[0].raw_name == "Claude"
    assert rec.models[0].slug == "claude"
    assert rec.models[0].messages == 10


def test_utf8_bom_stripped() -> None:
    data = af.build_report_csv([af.report_row()], add_bom=True)
    out = _parse(data)
    assert out.records
    assert out.records[0].user_email == "person1@example.test"


def test_quoted_commas_and_newlines() -> None:
    row = af.report_row(subscription_tier='Pro, "Plus"\nTeam')
    data = af.build_report_csv([row])
    out = _parse(data)
    assert out.records
    assert out.records[0].subscription_tier == 'Pro, "Plus"\nTeam'


def test_uppercase_and_mixed_case_headers_resolved() -> None:
    order = [h.upper() for h in af.FIXED_HEADERS]
    row = af.report_row()
    data = af.build_report_csv([row], header_order=order)
    out = _parse(data)
    assert out.records
    assert not any(i.severity is IssueSeverity.ERROR for i in out.issues)


def test_reordered_headers_and_unknown_columns_tolerated() -> None:
    order = list(reversed(af.FIXED_HEADERS)) + ["Random_Extra", "Notes"]
    row = af.report_row()
    row["Random_Extra"] = "ignored"
    row["Notes"] = "whatever"
    data = af.build_report_csv([row], header_order=order)
    out = _parse(data)
    assert out.records
    assert IssueCode.NO_MODEL_COLUMNS in _codes(out.issues)


def test_optional_usage_limit_parsed() -> None:
    row = af.report_row()
    row["Usage_Limit"] = "2000"
    data = af.build_report_csv([row], optional_usage_limit=True)
    out = _parse(data)
    assert out.records
    assert out.records[0].usage_limit == Decimal("2000")


def test_optional_usage_limit_blank_is_none() -> None:
    row = af.report_row()
    row["Usage_Limit"] = ""
    data = af.build_report_csv([row], optional_usage_limit=True)
    out = _parse(data)
    assert out.records
    assert out.records[0].usage_limit is None


def test_varying_model_columns_and_slug_normalisation() -> None:
    row = af.report_row(total_messages="5")
    row["Claude 3.5 Sonnet_messages"] = "5"
    data = af.build_report_csv([row], model_headers=["Claude 3.5 Sonnet_messages"])
    out = _parse(data)
    assert out.records
    m = out.records[0].models[0]
    assert m.raw_name == "Claude 3.5 Sonnet"
    assert m.slug == "claude-3-5-sonnet"


def test_uppercase_messages_suffix_recognised() -> None:
    row = af.report_row(total_messages="3")
    row["Nova_MESSAGES"] = "3"
    data = af.build_report_csv([row], model_headers=["Nova_MESSAGES"])
    out = _parse(data)
    assert out.records
    assert out.records[0].models[0].raw_name == "Nova"


def test_zero_count_model_omitted() -> None:
    row = af.report_row(total_messages="0")
    row["Claude_messages"] = "0"
    data = af.build_report_csv([row], model_headers=["Claude_messages"])
    out = _parse(data)
    assert out.records
    assert out.records[0].models == ()


def test_models_sorted_deterministically() -> None:
    row = af.report_row(total_messages="6")
    row["Zeta_messages"] = "1"
    row["Alpha_messages"] = "5"
    data = af.build_report_csv(
        [row], model_headers=["Zeta_messages", "Alpha_messages"]
    )
    out = _parse(data)
    slugs = [m.slug for m in out.records[0].models]
    assert slugs == sorted(slugs)
    assert slugs[0] == "alpha"


def test_ambiguous_model_headers_rejected() -> None:
    row = af.report_row(total_messages="2")
    data = af.build_report_csv(
        [row], model_headers=["Claude-X_messages", "Claude.X_messages"]
    )
    out = _parse(data)
    assert IssueCode.AMBIGUOUS_MODEL_HEADER in _codes(out.issues)
    assert not out.records


def test_missing_required_header_rejected() -> None:
    order = [h for h in af.FIXED_HEADERS if h != "Credits_Used"]
    data = af.build_report_csv([af.report_row()], header_order=order)
    out = _parse(data)
    assert IssueCode.MISSING_REQUIRED_HEADER in _codes(out.issues)
    assert not out.records


def test_duplicate_header_rejected() -> None:
    order = [*af.FIXED_HEADERS, "Date"]
    data = af.build_report_csv([af.report_row()], header_order=order)
    out = _parse(data)
    assert IssueCode.DUPLICATE_HEADER in _codes(out.issues)


def test_blank_header_rejected() -> None:
    order = [*af.FIXED_HEADERS, ""]
    data = af.build_report_csv([af.report_row()], header_order=order)
    out = _parse(data)
    assert IssueCode.BLANK_HEADER in _codes(out.issues)


def test_missing_cells_rejected_as_wrong_field_count() -> None:
    good = af.build_report_csv([af.report_row()])
    header_line, data_line = good.split(b"\n", 1)
    # Drop the final cell from the data row.
    truncated = header_line + b"\n" + data_line.rsplit(b",", 1)[0] + b"\n"
    out = _parse(truncated)
    assert IssueCode.WRONG_FIELD_COUNT in _codes(out.issues)
    assert not out.records


def test_extra_cells_rejected_as_wrong_field_count() -> None:
    good = af.build_report_csv([af.report_row()])
    header_line, rest = good.split(b"\n", 1)
    data_line = rest.rstrip(b"\n")
    extended = header_line + b"\n" + data_line + b",EXTRA\n"
    out = _parse(extended)
    assert IssueCode.WRONG_FIELD_COUNT in _codes(out.issues)


def test_invalid_utf8_rejected() -> None:
    data = b"\xff\xfe not valid utf-8 at all"
    out = _parse(data)
    assert IssueCode.INVALID_ENCODING in _codes(out.issues)
    assert not out.records


def test_empty_file_rejected() -> None:
    out = _parse(b"")
    assert IssueCode.EMPTY_FILE in _codes(out.issues)


def test_header_only_file_warns() -> None:
    data = af.build_report_csv([])
    out = _parse(data)
    assert IssueCode.HEADER_ONLY in _codes(out.issues)
    assert not out.records


def test_negative_decimal_rejected() -> None:
    row = af.report_row(credits_used="-1")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert IssueCode.INVALID_VALUE in _codes(out.issues)
    assert not out.records


def test_nan_rejected() -> None:
    row = af.report_row(credits_used="NaN")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert IssueCode.INVALID_VALUE in _codes(out.issues)


def test_infinity_rejected() -> None:
    row = af.report_row(credits_used="Infinity")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert IssueCode.INVALID_VALUE in _codes(out.issues)


def test_decimal_in_int_field_rejected() -> None:
    row = af.report_row(total_messages="10.5")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert IssueCode.INVALID_VALUE in _codes(out.issues)


def test_bad_bool_rejected() -> None:
    row = af.report_row(overage_enabled="yes")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert IssueCode.INVALID_VALUE in _codes(out.issues)


def test_bool_case_insensitive_accepted() -> None:
    row = af.report_row(overage_enabled="TRUE", new_user="False")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert out.records
    assert out.records[0].overage_enabled is True
    assert out.records[0].new_user is False


def test_bad_date_rejected() -> None:
    row = af.report_row(date="2026-13-40")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert IssueCode.INVALID_VALUE in _codes(out.issues)


def test_non_iso_date_rejected() -> None:
    row = af.report_row(date="01/01/2026")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert IssueCode.INVALID_VALUE in _codes(out.issues)


def test_empty_identity_rejected() -> None:
    row = af.report_row(user_email="")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert IssueCode.EMPTY_IDENTITY in _codes(out.issues)
    assert not out.records


def test_unknown_client_type_rejected() -> None:
    row = af.report_row(client_type="KIRO_UNKNOWN")
    data = af.build_report_csv([row])
    out = _parse(data)
    assert IssueCode.INVALID_VALUE in _codes(out.issues)


def test_message_sum_mismatch_warns_but_accepts() -> None:
    row = af.report_row(total_messages="99")
    row["Claude_messages"] = "5"
    data = af.build_report_csv([row], model_headers=["Claude_messages"])
    out = _parse(data)
    assert out.records  # accepted
    assert IssueCode.MESSAGE_SUM_MISMATCH in _codes(out.issues)


def test_no_model_columns_warns() -> None:
    data = af.build_report_csv([af.report_row()])
    out = _parse(data)
    assert IssueCode.NO_MODEL_COLUMNS in _codes(out.issues)


def test_row_limit_per_csv_enforced() -> None:
    rows = [af.report_row(user_id=f"user-{i}") for i in range(5)]
    data = af.build_report_csv(rows)
    limits = CsvLimits(max_rows_per_csv=2)
    out = _parse(data, limits=limits)
    assert len(out.records) == 2
    assert IssueCode.ROW_LIMIT_EXCEEDED in _codes(out.issues)


def test_import_row_limit_enforced() -> None:
    rows = [af.report_row(user_id=f"user-{i}") for i in range(5)]
    data = af.build_report_csv(rows)
    out = _parse(data, remaining=2)
    assert len(out.records) == 2
    assert IssueCode.IMPORT_ROW_LIMIT_EXCEEDED in _codes(out.issues)


def test_issue_messages_contain_no_pii() -> None:
    secret_email = "secret@example.test"
    secret_profile = "profile-secret-xyz"
    row = af.report_row(
        user_email=secret_email, profile_id=secret_profile, credits_used="-1"
    )
    data = af.build_report_csv([row])
    out = _parse(data)
    for issue in out.issues:
        assert secret_email not in issue.message
        assert secret_profile not in issue.message


def test_ide_and_plugin_client_types() -> None:
    for client in ("KIRO_IDE", "PLUGIN"):
        row = af.report_row(client_type=client)
        data = af.build_report_csv([row])
        out = _parse(data)
        assert out.records
        assert out.records[0].client_type.value == client


def test_data_issue_repr_has_no_pii() -> None:
    secret_email = "leak-repr@example.test"
    secret_profile = "profile-leak-repr"
    row = af.report_row(
        user_email=secret_email, profile_id=secret_profile, overage_enabled="maybe"
    )
    data = af.build_report_csv([row])
    out = _parse(data)
    for issue in out.issues:
        text = repr(issue)
        assert secret_email not in text
        assert secret_profile not in text
