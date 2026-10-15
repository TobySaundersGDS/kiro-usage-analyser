"""Import service: turn a validated archive into an immutable ImportResult.

This orchestrates the Task-1 safe archive reader and the Task-2 CSV parser:

1. Enumerate only the ACCEPTED CSV members (never extract).
2. Read each member's bytes one at a time with a hard actual-byte bound.
3. Parse each member into canonical records and safe issues.
4. Detect exact-duplicate rows (ignored, counted, warned).
5. Resolve logical-key conflicts deterministically (latest report timestamp,
   then member name, then index) without ever summing conflicting rows.
6. Compute safe aggregate metadata.

No raw bytes, streams, or DictReader rows are retained after parsing. No PII
or upload content is logged.
"""

from __future__ import annotations

import io
import zipfile

from ..adapters.archive import (
    ArchiveSummary,
    MemberReadError,
    iter_accepted_csv_members,
    parse_report_timestamp,
    read_member_bounded,
)
from ..adapters.kiro_csv import parse_member_csv
from ..config import ArchiveLimits, CsvLimits
from ..domain.usage import (
    ArchiveSummaryData,
    ClientType,
    DataIssue,
    ImportResult,
    IssueCode,
    IssueSeverity,
    SourceRef,
    UsageRecord,
)


def _normalise_timestamp(ts: str | None) -> str:
    """Normalise a report timestamp to a fixed-width comparable string.

    12-digit stamps are right-padded with ``00`` for seconds so a 12-digit and
    14-digit stamp for the same minute compare equal on the minute. ``None``
    sorts earliest (empty string).
    """

    if ts is None:
        return ""
    if len(ts) == 12:
        return ts + "00"
    return ts


def _conflict_sort_key(record: UsageRecord) -> tuple[str, str, int]:
    """Deterministic winner selection within a logical key.

    Latest report timestamp wins; ties broken by member name then index. A
    higher tuple wins, so we select ``max`` by this key.
    """

    source = record.source
    ts = _normalise_timestamp(source.report_timestamp if source else None)
    member = source.member if source else ""
    index = source.index if source else -1
    return (ts, member, index)


def build_import_result(
    data: bytes, archive: ArchiveSummary, csv_limits: CsvLimits
) -> ImportResult:
    """Parse all accepted CSV members and resolve into an ``ImportResult``.

    ``data`` is the uploaded archive bytes. They are read one member at a time
    and are not retained after this function returns. ``archive`` is the
    Task-1 validation summary, carried through for display.
    """

    records: list[UsageRecord] = []
    issues: list[DataIssue] = []
    accepted_rows = 0
    rejected_rows = 0

    members = iter_accepted_csv_members(data, ArchiveLimits())

    buffer = io.BytesIO(data)
    with zipfile.ZipFile(buffer) as zf:
        for index, display_name, info in members:
            source = SourceRef(
                member=display_name,
                index=index,
                report_timestamp=parse_report_timestamp(display_name),
            )
            try:
                raw = read_member_bounded(zf, info, csv_limits.max_member_read_bytes)
            except MemberReadError as exc:
                issues.append(
                    DataIssue(
                        severity=IssueSeverity.ERROR,
                        code=IssueCode.MEMBER_UNREADABLE,
                        message=f"A report member could not be read: {exc.reason}.",
                        source=source,
                    )
                )
                continue

            remaining = csv_limits.max_rows_per_import - accepted_rows
            output = parse_member_csv(
                raw, source, csv_limits, remaining_import_rows=max(remaining, 0)
            )
            # Release member bytes promptly.
            del raw

            issues.extend(output.issues)
            # Count every error issue that corresponds to a rejected data row.
            for issue in output.issues:
                if issue.row is not None and issue.severity is IssueSeverity.ERROR:
                    rejected_rows += 1
            records.extend(output.records)
            accepted_rows += len(output.records)

    deduped, dup_count, dup_issues = _dedupe_exact(records)
    issues.extend(dup_issues)

    resolved, conflict_count, conflict_issues = _resolve_conflicts(deduped)
    issues.extend(conflict_issues)

    aggregate = _aggregate(resolved, accepted_rows=len(resolved), rejected_rows=rejected_rows)

    return ImportResult(
        archive=archive,
        records=tuple(resolved),
        issues=tuple(issues),
        duplicate_count=dup_count,
        conflict_count=conflict_count,
        aggregate=aggregate,
    )


def _dedupe_exact(
    records: list[UsageRecord],
) -> tuple[list[UsageRecord], int, list[DataIssue]]:
    """Drop content-identical duplicate rows, keeping the first occurrence."""

    seen: set[tuple[object, ...]] = set()
    kept: list[UsageRecord] = []
    duplicates = 0
    for record in records:
        fp = record.content_fingerprint()
        if fp in seen:
            duplicates += 1
            continue
        seen.add(fp)
        kept.append(record)
    issues: list[DataIssue] = []
    if duplicates:
        issues.append(
            DataIssue(
                severity=IssueSeverity.WARNING,
                code=IssueCode.DUPLICATE_ROW,
                message=(
                    f"{duplicates} exact-duplicate row(s) were ignored across the "
                    "archive."
                ),
            )
        )
    return kept, duplicates, issues


def _resolve_conflicts(
    records: list[UsageRecord],
) -> tuple[list[UsageRecord], int, list[DataIssue]]:
    """Resolve rows sharing a logical key; never sum them.

    Groups by logical key; when more than one *content-distinct* record shares
    a key, the deterministic winner (latest timestamp, then member, then index)
    is kept and the others discarded. Returns records in a normalised order.
    """

    groups: dict[tuple[str, str, str, ClientType], list[UsageRecord]] = {}
    for record in records:
        groups.setdefault(record.logical_key, []).append(record)

    resolved: list[UsageRecord] = []
    conflicts = 0
    for group in groups.values():
        if len(group) == 1:
            resolved.append(group[0])
            continue
        conflicts += len(group) - 1
        winner = max(group, key=_conflict_sort_key)
        resolved.append(winner)

    issues: list[DataIssue] = []
    if conflicts:
        issues.append(
            DataIssue(
                severity=IssueSeverity.WARNING,
                code=IssueCode.CONFLICTING_ROW,
                message=(
                    f"{conflicts} conflicting row(s) shared a daily key; the latest "
                    "report was kept and the rest were discarded."
                ),
            )
        )

    # Normalise final order deterministically by logical key then source.
    resolved.sort(
        key=lambda r: (
            r.date,
            str(r.client_type),
            r.profile_id,
            r.user_id,
        )
    )
    return resolved, conflicts, issues


def _aggregate(
    records: list[UsageRecord], *, accepted_rows: int, rejected_rows: int
) -> ArchiveSummaryData:
    """Compute safe aggregate metadata from the resolved records."""

    dates = sorted({r.date for r in records})
    distinct_users = len({r.user_id for r in records})
    subscription_count = len({r.subscription_key for r in records})
    client_types = frozenset(r.client_type for r in records)
    model_slugs = frozenset(m.slug for r in records for m in r.models)

    return ArchiveSummaryData(
        accepted_rows=accepted_rows,
        rejected_rows=rejected_rows,
        distinct_users=distinct_users,
        subscription_count=subscription_count,
        earliest_date=dates[0] if dates else None,
        latest_date=dates[-1] if dates else None,
        client_types=client_types,
        model_slugs=model_slugs,
    )


__all__ = ["build_import_result"]
