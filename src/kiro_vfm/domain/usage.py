"""Immutable canonical usage domain.

This module defines frozen, hashable value types that model Kiro's daily
user-activity reports after they have been parsed and validated. Everything
here is deliberately immutable: nested collections are ``tuple``/``frozenset``
rather than ``list``/``dict`` so that a parsed :class:`ImportResult` cannot be
mutated after construction and can be safely shared across an ephemeral
session.

PII handling
------------
Canonical :class:`UsageRecord` objects *do* retain personal data (``user_id``,
``profile_id``, ``user_email``) because downstream analysis needs them. That
data lives only in memory for the lifetime of a session. In contrast,
:class:`DataIssue` is a *safe* type: it must never carry raw cell values,
email, ``UserId`` or ``ProfileId``. Issues are the only usage-derived values
that are rendered broadly / could end up near logs, so they are kept generic
by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..adapters.archive import ArchiveSummary


class ClientType(StrEnum):
    """The Kiro client that produced a report row.

    The canonical set is fixed; unknown client values are rejected during
    parsing rather than silently coerced.
    """

    KIRO_CLI = "KIRO_CLI"
    KIRO_IDE = "KIRO_IDE"
    PLUGIN = "PLUGIN"


class IssueSeverity(StrEnum):
    """Severity of a data-quality issue."""

    WARNING = "warning"
    ERROR = "error"


class IssueCode(StrEnum):
    """Stable, generic machine codes for data-quality issues.

    Codes are intentionally coarse and carry no row-specific detail. The
    human-facing message attached to a :class:`DataIssue` is likewise generic.
    """

    # Whole-file / archive-member level.
    MEMBER_UNREADABLE = "member_unreadable"
    INVALID_ENCODING = "invalid_encoding"
    EMPTY_FILE = "empty_file"
    HEADER_ONLY = "header_only"
    DUPLICATE_HEADER = "duplicate_header"
    BLANK_HEADER = "blank_header"
    MISSING_REQUIRED_HEADER = "missing_required_header"
    AMBIGUOUS_MODEL_HEADER = "ambiguous_model_header"
    ROW_LIMIT_EXCEEDED = "row_limit_exceeded"
    IMPORT_ROW_LIMIT_EXCEEDED = "import_row_limit_exceeded"

    # Row level.
    WRONG_FIELD_COUNT = "wrong_field_count"
    INVALID_VALUE = "invalid_value"
    EMPTY_IDENTITY = "empty_identity"

    # Cross-row / aggregate level.
    DUPLICATE_ROW = "duplicate_row"
    CONFLICTING_ROW = "conflicting_row"
    MESSAGE_SUM_MISMATCH = "message_sum_mismatch"
    NO_MODEL_COLUMNS = "no_model_columns"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Safe provenance for a parsed value.

    ``member`` is the display-sanitised archive member name (never raw bytes).
    ``index`` is the member's deterministic position within the archive.
    ``report_timestamp`` is parsed from the member filename when present and is
    used only for deterministic conflict resolution; it is not PII.
    """

    member: str
    index: int
    report_timestamp: str | None = None

    @property
    def display_name(self) -> str:
        """A short, safe label for display."""

        return self.member


@dataclass(frozen=True, slots=True, order=True)
class ModelUsage:
    """Message count attributed to a single dynamic model column.

    ``raw_name`` preserves the identifier exactly as it appeared in the header
    (minus the ``_messages`` suffix). ``slug`` is a mechanical lowercase
    canonical form produced by normalising separators only — no aliasing,
    equivalence, or pricing is implied. Fields are ordered ``(slug, raw_name,
    messages)`` so tuples of model usage sort deterministically.

    Zero-count model usage is never represented: the parser omits it so that a
    harmless all-zero model column cannot affect exact-duplicate equality.
    """

    slug: str
    raw_name: str
    messages: int


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """A single canonical daily usage row for one (profile, user, client).

    Equality / hashing intentionally covers *content* only, excluding
    provenance (:attr:`source`), so two identical rows coming from different
    members compare equal for exact-duplicate detection.
    """

    date: str
    client_type: ClientType
    profile_id: str
    user_id: str
    user_email: str
    subscription_tier: str
    chat_conversations: int
    total_messages: int
    credits_used: Decimal
    overage_cap: Decimal
    overage_credits_used: Decimal
    overage_enabled: bool
    new_user: bool
    models: tuple[ModelUsage, ...]
    usage_limit: Decimal | None = None
    # Provenance is excluded from equality/hash so content-identical rows from
    # different members are detected as exact duplicates.
    source: SourceRef | None = field(default=None, compare=False)

    @property
    def logical_key(self) -> tuple[str, str, str, ClientType]:
        """Logical daily key: (ProfileId, UserId, Date, Client_Type)."""

        return (self.profile_id, self.user_id, self.date, self.client_type)

    @property
    def subscription_key(self) -> tuple[str, str]:
        """Subscription key: (ProfileId, UserId)."""

        return (self.profile_id, self.user_id)

    def content_fingerprint(
        self,
    ) -> tuple[object, ...]:
        """A hashable fingerprint of record *content* (no provenance).

        Used for exact-duplicate detection. Because zero-count models are
        never stored and :attr:`models` is kept sorted, two records that differ
        only by a harmless all-zero model column fingerprint identically.
        """

        return (
            self.date,
            self.client_type,
            self.profile_id,
            self.user_id,
            self.user_email,
            self.subscription_tier,
            self.chat_conversations,
            self.total_messages,
            self.credits_used,
            self.overage_cap,
            self.overage_credits_used,
            self.overage_enabled,
            self.new_user,
            self.usage_limit,
            self.models,
        )


@dataclass(frozen=True, slots=True)
class DataIssue:
    """A safe, display-ready data-quality issue.

    Must never carry raw cell values, email, ``UserId`` or ``ProfileId``. The
    ``message`` is a generic human-readable sentence; ``code`` is the stable
    machine code; ``source`` names the member safely; ``row`` is the 1-based
    data row number when applicable (header is not counted).
    """

    severity: IssueSeverity
    code: IssueCode
    message: str
    source: SourceRef | None = None
    row: int | None = None


@dataclass(frozen=True, slots=True)
class ArchiveSummaryData:
    """Safe aggregate metadata derived from parsed records.

    Contains no PII beyond counts and distinct totals. ``client_types`` and
    ``model_slugs`` are frozensets of canonical labels (never raw cells).
    """

    accepted_rows: int
    rejected_rows: int
    distinct_users: int
    subscription_count: int
    earliest_date: str | None
    latest_date: str | None
    client_types: frozenset[ClientType]
    model_slugs: frozenset[str]


@dataclass(frozen=True, slots=True)
class ImportResult:
    """The complete, immutable outcome of importing an archive's CSV members.

    Holds the (already-validated) archive summary from Task 1, the canonical
    records, every data-quality issue, duplicate/conflict counts, and safe
    aggregate metadata. All nested collections are immutable.

    ``archive`` is typed loosely as :class:`object` here to avoid a domain ->
    adapter import cycle; in practice it is an
    :class:`kiro_vfm.adapters.archive.ArchiveSummary`, which is itself frozen
    with an immutable ``members`` tuple.
    """

    archive: ArchiveSummary
    records: tuple[UsageRecord, ...]
    issues: tuple[DataIssue, ...]
    duplicate_count: int
    conflict_count: int
    aggregate: ArchiveSummaryData

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is IssueSeverity.WARNING)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is IssueSeverity.ERROR)


__all__ = [
    "ClientType",
    "IssueSeverity",
    "IssueCode",
    "SourceRef",
    "ModelUsage",
    "UsageRecord",
    "DataIssue",
    "ArchiveSummaryData",
    "ImportResult",
]
