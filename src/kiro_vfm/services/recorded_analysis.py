"""Level 1 recorded-usage analysis.

This service computes **exact recorded facts** from the canonical usage
records held in an :class:`~kiro_vfm.domain.usage.ImportResult`. It performs
no estimation, pricing, token reconstruction, or value-for-money calculation —
only deterministic aggregation of values that appeared in the reports.

Design notes:

* All monetary / credit quantities stay as :class:`decimal.Decimal` so totals
  are exact; no float arithmetic is used anywhere.
* ``Total_Messages`` counts prompts, responses, and tool calls — it is **not**
  a count of API invocations. Callers surface this distinction in the UI.
* Results are returned as frozen, immutable dataclasses so a computed analysis
  cannot be mutated by the view layer.
* Filtering (date range, client type) is applied up front; every breakdown is
  computed over the already-filtered record set so KPIs and breakdowns always
  reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.usage import ClientType, ImportResult, UsageRecord

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class UsageFilter:
    """A recorded-usage filter.

    ``date_from``/``date_to`` are inclusive ISO ``YYYY-MM-DD`` bounds (string
    comparison is valid for zero-padded ISO dates). ``client_type`` restricts
    to a single client when set. ``None`` fields mean "no bound".
    """

    date_from: str | None = None
    date_to: str | None = None
    client_type: ClientType | None = None

    def matches(self, record: UsageRecord) -> bool:
        if self.date_from is not None and record.date < self.date_from:
            return False
        if self.date_to is not None and record.date > self.date_to:
            return False
        return self.client_type is None or record.client_type is self.client_type

    @property
    def is_active(self) -> bool:
        return (
            self.date_from is not None
            or self.date_to is not None
            or self.client_type is not None
        )


@dataclass(frozen=True, slots=True)
class Totals:
    """Exact recorded KPI totals over a record set."""

    record_count: int
    distinct_users: int
    subscription_count: int
    distinct_days: int
    credits_used: Decimal
    overage_credits_used: Decimal
    total_messages: int
    chat_conversations: int
    model_messages: int


@dataclass(frozen=True, slots=True)
class DateBreakdownRow:
    date: str
    record_count: int
    distinct_users: int
    credits_used: Decimal
    total_messages: int
    chat_conversations: int


@dataclass(frozen=True, slots=True)
class PersonBreakdownRow:
    """Per-subscription recorded facts.

    Keyed by the subscription key ``(ProfileId, UserId)``. Email, profile id
    and the highest observed tier are retained because Level 1 person views are
    allowed to display identity. ``tiers`` lists every distinct tier observed
    for the subscription so tier changes are visible.
    """

    profile_id: str
    user_id: str
    user_email: str
    tiers: tuple[str, ...]
    client_types: tuple[ClientType, ...]
    record_count: int
    credits_used: Decimal
    overage_credits_used: Decimal
    total_messages: int
    chat_conversations: int


@dataclass(frozen=True, slots=True)
class ClientBreakdownRow:
    client_type: ClientType
    record_count: int
    distinct_users: int
    credits_used: Decimal
    total_messages: int
    model_messages: int


@dataclass(frozen=True, slots=True)
class ModelBreakdownRow:
    """Recorded per-model message counts.

    ``slug`` is the mechanical canonical slug; ``raw_names`` collects every
    raw header identifier that mapped to this slug (there is normally one).
    Only recorded message counts are shown — no pricing or token estimate.
    """

    slug: str
    raw_names: tuple[str, ...]
    messages: int
    distinct_users: int


@dataclass(frozen=True, slots=True)
class RecordedAnalysis:
    """The complete, immutable Level 1 recorded-usage analysis."""

    applied_filter: UsageFilter
    totals: Totals
    by_date: tuple[DateBreakdownRow, ...]
    by_person: tuple[PersonBreakdownRow, ...]
    by_client: tuple[ClientBreakdownRow, ...]
    by_model: tuple[ModelBreakdownRow, ...]
    # Distinct values available for building filter controls (from the full,
    # unfiltered record set so a user can always widen a filter again).
    available_dates: tuple[str, ...]
    available_clients: tuple[ClientType, ...]

    @property
    def has_records(self) -> bool:
        return self.totals.record_count > 0


def analyse(result: ImportResult, usage_filter: UsageFilter | None = None) -> RecordedAnalysis:
    """Compute the Level 1 recorded-usage analysis for ``result``.

    ``usage_filter`` restricts the records considered; when ``None`` the whole
    record set is analysed.
    """

    usage_filter = usage_filter or UsageFilter()
    all_records = result.records
    records = tuple(r for r in all_records if usage_filter.matches(r))

    totals = _totals(records)
    return RecordedAnalysis(
        applied_filter=usage_filter,
        totals=totals,
        by_date=_by_date(records),
        by_person=_by_person(records),
        by_client=_by_client(records),
        by_model=_by_model(records),
        available_dates=tuple(sorted({r.date for r in all_records})),
        available_clients=tuple(
            sorted({r.client_type for r in all_records}, key=lambda c: c.value)
        ),
    )


def _totals(records: tuple[UsageRecord, ...]) -> Totals:
    credits = sum((r.credits_used for r in records), _ZERO)
    overage = sum((r.overage_credits_used for r in records), _ZERO)
    total_messages = sum(r.total_messages for r in records)
    conversations = sum(r.chat_conversations for r in records)
    model_messages = sum(m.messages for r in records for m in r.models)
    return Totals(
        record_count=len(records),
        distinct_users=len({r.user_id for r in records}),
        subscription_count=len({r.subscription_key for r in records}),
        distinct_days=len({r.date for r in records}),
        credits_used=credits,
        overage_credits_used=overage,
        total_messages=total_messages,
        chat_conversations=conversations,
        model_messages=model_messages,
    )


def _by_date(records: tuple[UsageRecord, ...]) -> tuple[DateBreakdownRow, ...]:
    buckets: dict[str, list[UsageRecord]] = {}
    for r in records:
        buckets.setdefault(r.date, []).append(r)
    rows = [
        DateBreakdownRow(
            date=date,
            record_count=len(group),
            distinct_users=len({r.user_id for r in group}),
            credits_used=sum((r.credits_used for r in group), _ZERO),
            total_messages=sum(r.total_messages for r in group),
            chat_conversations=sum(r.chat_conversations for r in group),
        )
        for date, group in buckets.items()
    ]
    rows.sort(key=lambda row: row.date)
    return tuple(rows)


def _by_person(records: tuple[UsageRecord, ...]) -> tuple[PersonBreakdownRow, ...]:
    buckets: dict[tuple[str, str], list[UsageRecord]] = {}
    for r in records:
        buckets.setdefault(r.subscription_key, []).append(r)

    rows: list[PersonBreakdownRow] = []
    for (profile_id, user_id), group in buckets.items():
        # Deterministic email choice: the latest record by date, then source.
        latest = max(
            group,
            key=lambda r: (
                r.date,
                r.source.index if r.source else -1,
            ),
        )
        tiers = tuple(sorted({r.subscription_tier for r in group}))
        clients = tuple(sorted({r.client_type for r in group}, key=lambda c: c.value))
        rows.append(
            PersonBreakdownRow(
                profile_id=profile_id,
                user_id=user_id,
                user_email=latest.user_email,
                tiers=tiers,
                client_types=clients,
                record_count=len(group),
                credits_used=sum((r.credits_used for r in group), _ZERO),
                overage_credits_used=sum((r.overage_credits_used for r in group), _ZERO),
                total_messages=sum(r.total_messages for r in group),
                chat_conversations=sum(r.chat_conversations for r in group),
            )
        )
    # Order by credits used (desc) then email for a stable, useful default.
    rows.sort(key=lambda row: (-row.credits_used, row.user_email, row.profile_id))
    return tuple(rows)


def _by_client(records: tuple[UsageRecord, ...]) -> tuple[ClientBreakdownRow, ...]:
    buckets: dict[ClientType, list[UsageRecord]] = {}
    for r in records:
        buckets.setdefault(r.client_type, []).append(r)
    rows = [
        ClientBreakdownRow(
            client_type=client,
            record_count=len(group),
            distinct_users=len({r.user_id for r in group}),
            credits_used=sum((r.credits_used for r in group), _ZERO),
            total_messages=sum(r.total_messages for r in group),
            model_messages=sum(m.messages for r in group for m in r.models),
        )
        for client, group in buckets.items()
    ]
    rows.sort(key=lambda row: row.client_type.value)
    return tuple(rows)


def _by_model(records: tuple[UsageRecord, ...]) -> tuple[ModelBreakdownRow, ...]:
    messages: dict[str, int] = {}
    raw_names: dict[str, set[str]] = {}
    users: dict[str, set[str]] = {}
    for r in records:
        for m in r.models:
            messages[m.slug] = messages.get(m.slug, 0) + m.messages
            raw_names.setdefault(m.slug, set()).add(m.raw_name)
            users.setdefault(m.slug, set()).add(r.user_id)
    rows = [
        ModelBreakdownRow(
            slug=slug,
            raw_names=tuple(sorted(raw_names[slug])),
            messages=count,
            distinct_users=len(users[slug]),
        )
        for slug, count in messages.items()
    ]
    # Order by messages (desc) then slug for determinism.
    rows.sort(key=lambda row: (-row.messages, row.slug))
    return tuple(rows)


__all__ = [
    "UsageFilter",
    "Totals",
    "DateBreakdownRow",
    "PersonBreakdownRow",
    "ClientBreakdownRow",
    "ModelBreakdownRow",
    "RecordedAnalysis",
    "analyse",
]
