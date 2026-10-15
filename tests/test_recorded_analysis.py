"""Unit tests for the Level 1 recorded-usage analysis service and view models.

All identities are synthetic and use the reserved example.test domain. These
tests build canonical records directly so they exercise aggregation without
going through CSV parsing.
"""

from __future__ import annotations

from decimal import Decimal

from kiro_vfm.adapters.archive import ArchiveSummary
from kiro_vfm.domain.usage import (
    ArchiveSummaryData,
    ClientType,
    ImportResult,
    ModelUsage,
    SourceRef,
    UsageRecord,
)
from kiro_vfm.services.recorded_analysis import UsageFilter, analyse
from kiro_vfm.web.view_models import build_filter_view, format_decimal, parse_filter


def _record(
    *,
    date: str = "2026-01-01",
    client: ClientType = ClientType.KIRO_CLI,
    profile: str = "p1",
    user: str = "u1",
    email: str = "a@example.test",
    tier: str = "Pro",
    conversations: int = 1,
    total_messages: int = 10,
    credits: str = "1.0",
    overage_credits: str = "0",
    models: tuple[ModelUsage, ...] = (),
    index: int = 0,
) -> UsageRecord:
    return UsageRecord(
        date=date,
        client_type=client,
        profile_id=profile,
        user_id=user,
        user_email=email,
        subscription_tier=tier,
        chat_conversations=conversations,
        total_messages=total_messages,
        credits_used=Decimal(credits),
        overage_cap=Decimal(0),
        overage_credits_used=Decimal(overage_credits),
        overage_enabled=False,
        new_user=False,
        models=models,
        usage_limit=None,
        source=SourceRef(member=f"m{index}.csv", index=index),
    )


def _result(records: tuple[UsageRecord, ...]) -> ImportResult:
    archive = ArchiveSummary(1, len(records), len(records), 0, 0, len(records), ())
    agg = ArchiveSummaryData(
        accepted_rows=len(records),
        rejected_rows=0,
        distinct_users=len({r.user_id for r in records}),
        subscription_count=len({r.subscription_key for r in records}),
        earliest_date=min((r.date for r in records), default=None),
        latest_date=max((r.date for r in records), default=None),
        client_types=frozenset(r.client_type for r in records),
        model_slugs=frozenset(m.slug for r in records for m in r.models),
    )
    return ImportResult(
        archive=archive,
        records=records,
        issues=(),
        duplicate_count=0,
        conflict_count=0,
        aggregate=agg,
    )


# --- Totals -----------------------------------------------------------------


def test_totals_exact_decimal_sum() -> None:
    records = (
        _record(credits="1.5", user="u1"),
        _record(credits="0.25", user="u2", index=1),
        _record(credits="0.1", user="u3", index=2),
    )
    analysis = analyse(_result(records))
    # Exact decimal arithmetic: 1.5 + 0.25 + 0.1 == 1.85 (no float drift).
    assert analysis.totals.credits_used == Decimal("1.85")
    assert analysis.totals.record_count == 3
    assert analysis.totals.distinct_users == 3


def test_totals_message_and_conversation_counts() -> None:
    records = (
        _record(total_messages=10, conversations=2),
        _record(total_messages=5, conversations=1, user="u2", index=1),
    )
    analysis = analyse(_result(records))
    assert analysis.totals.total_messages == 15
    assert analysis.totals.chat_conversations == 3


def test_subscription_count_distinct_from_users() -> None:
    # Same UserId under two profiles -> 1 user, 2 subscriptions.
    records = (
        _record(user="u1", profile="p1"),
        _record(user="u1", profile="p2", index=1),
    )
    analysis = analyse(_result(records))
    assert analysis.totals.distinct_users == 1
    assert analysis.totals.subscription_count == 2


# --- Breakdowns -------------------------------------------------------------


def test_by_date_sorted_and_aggregated() -> None:
    records = (
        _record(date="2026-01-02", credits="2", user="u1"),
        _record(date="2026-01-01", credits="1", user="u2", index=1),
        _record(date="2026-01-01", credits="3", user="u3", index=2),
    )
    analysis = analyse(_result(records))
    dates = [r.date for r in analysis.by_date]
    assert dates == ["2026-01-01", "2026-01-02"]
    assert analysis.by_date[0].credits_used == Decimal("4")
    assert analysis.by_date[0].distinct_users == 2


def test_by_person_keyed_by_subscription_with_email_and_tiers() -> None:
    records = (
        _record(user="u1", profile="p1", tier="Pro", credits="1", date="2026-01-01"),
        _record(
            user="u1",
            profile="p1",
            tier="Pro Max",
            credits="2",
            date="2026-01-02",
            email="latest@example.test",
            index=1,
        ),
    )
    analysis = analyse(_result(records))
    assert len(analysis.by_person) == 1
    person = analysis.by_person[0]
    assert person.credits_used == Decimal("3")
    assert person.tiers == ("Pro", "Pro Max")  # both observed tiers
    assert person.user_email == "latest@example.test"  # latest record wins


def test_by_client_aggregated() -> None:
    records = (
        _record(client=ClientType.KIRO_CLI, credits="1", user="u1"),
        _record(client=ClientType.KIRO_IDE, credits="2", user="u2", index=1),
        _record(client=ClientType.KIRO_CLI, credits="3", user="u3", index=2),
    )
    analysis = analyse(_result(records))
    by_client = {r.client_type: r for r in analysis.by_client}
    assert by_client[ClientType.KIRO_CLI].credits_used == Decimal("4")
    assert by_client[ClientType.KIRO_CLI].distinct_users == 2
    assert by_client[ClientType.KIRO_IDE].credits_used == Decimal("2")


def test_by_model_aggregates_slugs() -> None:
    records = (
        _record(models=(ModelUsage("claude", "Claude", 5),), user="u1"),
        _record(models=(ModelUsage("claude", "Claude", 3),), user="u2", index=1),
        _record(models=(ModelUsage("nova", "Nova", 2),), user="u3", index=2),
    )
    analysis = analyse(_result(records))
    by_slug = {r.slug: r for r in analysis.by_model}
    assert by_slug["claude"].messages == 8
    assert by_slug["claude"].distinct_users == 2
    # Ordered by messages desc.
    assert analysis.by_model[0].slug == "claude"


def test_by_model_empty_when_no_models() -> None:
    analysis = analyse(_result((_record(),)))
    assert analysis.by_model == ()


# --- Filtering --------------------------------------------------------------


def test_filter_by_date_range() -> None:
    records = (
        _record(date="2026-01-01", credits="1", user="u1"),
        _record(date="2026-01-02", credits="2", user="u2", index=1),
        _record(date="2026-01-03", credits="4", user="u3", index=2),
    )
    analysis = analyse(_result(records), UsageFilter(date_from="2026-01-02"))
    assert analysis.totals.credits_used == Decimal("6")
    assert analysis.totals.record_count == 2


def test_filter_by_client() -> None:
    records = (
        _record(client=ClientType.KIRO_CLI, credits="1", user="u1"),
        _record(client=ClientType.KIRO_IDE, credits="2", user="u2", index=1),
    )
    analysis = analyse(_result(records), UsageFilter(client_type=ClientType.KIRO_IDE))
    assert analysis.totals.credits_used == Decimal("2")
    assert analysis.totals.record_count == 1


def test_filter_to_date_inclusive() -> None:
    records = (
        _record(date="2026-01-01", credits="1", user="u1"),
        _record(date="2026-01-02", credits="2", user="u2", index=1),
    )
    analysis = analyse(_result(records), UsageFilter(date_to="2026-01-01"))
    assert analysis.totals.record_count == 1
    assert analysis.totals.credits_used == Decimal("1")


def test_available_options_from_full_set_even_when_filtered() -> None:
    records = (
        _record(date="2026-01-01", client=ClientType.KIRO_CLI, user="u1"),
        _record(date="2026-01-02", client=ClientType.KIRO_IDE, user="u2", index=1),
    )
    analysis = analyse(_result(records), UsageFilter(client_type=ClientType.KIRO_CLI))
    # Only one record matches, but both clients/dates remain selectable.
    assert analysis.available_clients == (ClientType.KIRO_CLI, ClientType.KIRO_IDE)
    assert analysis.available_dates == ("2026-01-01", "2026-01-02")


def test_empty_result_has_no_records() -> None:
    analysis = analyse(_result(()))
    assert not analysis.has_records
    assert analysis.totals.credits_used == Decimal(0)


# --- View models ------------------------------------------------------------


def test_format_decimal_trims_trailing_zeros() -> None:
    assert format_decimal(Decimal("1.50")) == "1.5"
    assert format_decimal(Decimal("2.00")) == "2"
    assert format_decimal(Decimal("0")) == "0"
    assert format_decimal(Decimal("0.125")) == "0.125"


def test_format_decimal_no_scientific_notation() -> None:
    assert format_decimal(Decimal("1E+2")) == "100"
    assert format_decimal(Decimal("1000000")) == "1000000"


def test_parse_filter_valid() -> None:
    f = parse_filter(date_from="2026-01-01", date_to="2026-01-31", client="KIRO_IDE")
    assert f.date_from == "2026-01-01"
    assert f.date_to == "2026-01-31"
    assert f.client_type is ClientType.KIRO_IDE
    assert f.is_active


def test_parse_filter_invalid_ignored() -> None:
    f = parse_filter(date_from="nonsense", date_to="2026/01/31", client="BOGUS")
    assert f.date_from is None
    assert f.date_to is None
    assert f.client_type is None
    assert not f.is_active


def test_build_filter_view_min_max_dates() -> None:
    records = (
        _record(date="2026-01-01", user="u1"),
        _record(date="2026-01-05", user="u2", index=1),
    )
    analysis = analyse(_result(records))
    view = build_filter_view(analysis)
    assert view.min_date == "2026-01-01"
    assert view.max_date == "2026-01-05"
    assert view.available_clients == ("KIRO_CLI",)
