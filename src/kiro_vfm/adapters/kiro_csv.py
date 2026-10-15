"""Parse a single Kiro daily user-activity report CSV into canonical records.

The report schema *changes* between days and clients: a fixed set of columns is
always present (resolved case-insensitively), ``Usage_Limit`` is optional, and
any column whose name ends (case-insensitively) with ``_messages`` is a dynamic
per-model message count.

This module is deliberately strict and *safe by construction*:

* It never raises on malformed input for an individual file or row — instead it
  emits generic, PII-free :class:`DataIssue` values and skips the offending row
  or file. Only programmer errors would raise.
* It never places raw cell values, email, ``UserId`` or ``ProfileId`` in any
  issue message or exception.
* It uses only the standard library (:mod:`csv`, :mod:`io`, :mod:`decimal`).

Parsing operates on bytes for one member at a time. The caller (the archive
import service) is responsible for bounding how many bytes are read.
"""

from __future__ import annotations

import csv
import datetime
import io
import re
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation

from ..config import CsvLimits
from ..domain.usage import (
    ClientType,
    DataIssue,
    IssueCode,
    IssueSeverity,
    ModelUsage,
    SourceRef,
    UsageRecord,
)

# Fixed required headers (canonical spelling). Resolution is case-insensitive.
_REQUIRED_HEADERS: tuple[str, ...] = (
    "Date",
    "UserId",
    "Client_Type",
    "Chat_Conversations",
    "Credits_Used",
    "Overage_Cap",
    "Overage_Credits_Used",
    "Overage_Enabled",
    "ProfileId",
    "Subscription_Tier",
    "Total_Messages",
    "New_User",
    "User_Email",
)
_OPTIONAL_HEADERS: tuple[str, ...] = ("Usage_Limit",)

_MODEL_SUFFIX = "_messages"

# ISO date: YYYY-MM-DD with a real calendar check done separately.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Separator normalisation for the mechanical model slug: collapse any run of
# non-alphanumeric characters to a single hyphen.
_SLUG_SEP_RE = re.compile(r"[^a-z0-9]+")


class _RowError(Exception):
    """Internal control-flow signal: a single data row is invalid.

    Carries only a generic, safe message and code — never raw cell content.
    """

    def __init__(self, code: IssueCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _slugify(raw_name: str) -> str:
    """Mechanical lowercase canonical slug: normalise separators only."""

    lowered = raw_name.lower()
    collapsed = _SLUG_SEP_RE.sub("-", lowered)
    return collapsed.strip("-")


def _parse_decimal(value: str, *, allow_optional: bool = False) -> Decimal | None:
    """Parse a finite, non-negative :class:`Decimal`.

    Rejects NaN/Infinity (even though they are syntactically valid Decimals),
    negatives, and anything non-numeric. Returns ``None`` only when
    ``allow_optional`` is set and the cell is blank.
    """

    text = value.strip()
    if text == "":
        if allow_optional:
            return None
        raise _RowError(IssueCode.INVALID_VALUE, "a numeric field was empty")
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise _RowError(
            IssueCode.INVALID_VALUE, "a numeric field was not a valid number"
        ) from exc
    if not parsed.is_finite():
        raise _RowError(IssueCode.INVALID_VALUE, "a numeric field was not finite")
    if parsed < 0:
        raise _RowError(IssueCode.INVALID_VALUE, "a numeric field was negative")
    return parsed


def _parse_int(value: str) -> int:
    """Parse a strict non-negative integer.

    Rejects decimals, signs, whitespace-wrapped junk, and non-digits. A plain
    run of digits (optionally with surrounding whitespace) is required.
    """

    text = value.strip()
    if text == "" or not text.isascii() or not text.isdigit():
        raise _RowError(
            IssueCode.INVALID_VALUE, "a count field was not a non-negative integer"
        )
    return int(text)


def _parse_bool(value: str) -> bool:
    """Parse only ``true``/``false`` (case-insensitive)."""

    text = value.strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise _RowError(IssueCode.INVALID_VALUE, "a boolean field was not true/false")


def _parse_date(value: str) -> str:
    """Validate an ISO ``YYYY-MM-DD`` date and return it normalised."""

    text = value.strip()
    if not _ISO_DATE_RE.match(text):
        raise _RowError(IssueCode.INVALID_VALUE, "the date was not ISO YYYY-MM-DD")
    year, month, day = (int(p) for p in text.split("-"))
    try:
        datetime.date(year, month, day)
    except ValueError as exc:
        raise _RowError(IssueCode.INVALID_VALUE, "the date was not a real date") from exc
    return text


def _parse_client_type(value: str) -> ClientType:
    text = value.strip()
    try:
        return ClientType(text)
    except ValueError as exc:
        raise _RowError(
            IssueCode.INVALID_VALUE, "the client type was not a known value"
        ) from exc


def _require_nonempty(value: str, label: str) -> str:
    text = value.strip()
    if text == "":
        raise _RowError(IssueCode.EMPTY_IDENTITY, f"the {label} was empty")
    return text


class _HeaderPlan:
    """Resolved header layout for one CSV member.

    Maps canonical required/optional field names to their column index and
    records the dynamic model columns (slug, raw name, index).
    """

    __slots__ = ("fixed", "optional", "models")

    def __init__(
        self,
        fixed: dict[str, int],
        optional: dict[str, int],
        models: tuple[tuple[str, str, int], ...],
    ) -> None:
        self.fixed = fixed
        self.optional = optional
        self.models = models


def _resolve_headers(header: list[str]) -> _HeaderPlan:
    """Resolve the header row or raise :class:`_RowError` with a safe code.

    Rejects blank headers, duplicate (case-insensitive) headers, missing
    required headers, and ambiguous duplicate model headers. Unknown extra
    columns are tolerated. Header order is irrelevant.
    """

    # Blank header cell.
    if any(col.strip() == "" for col in header):
        raise _RowError(IssueCode.BLANK_HEADER, "a header cell was blank")

    lowered = [col.strip().lower() for col in header]

    # Duplicate headers (case-insensitive) are ambiguous.
    seen: set[str] = set()
    for name in lowered:
        if name in seen:
            raise _RowError(
                IssueCode.DUPLICATE_HEADER, "a header name appeared more than once"
            )
        seen.add(name)

    index_by_lower = {name: i for i, name in enumerate(lowered)}

    fixed: dict[str, int] = {}
    for canonical in _REQUIRED_HEADERS:
        idx = index_by_lower.get(canonical.lower())
        if idx is None:
            raise _RowError(
                IssueCode.MISSING_REQUIRED_HEADER, "a required column was missing"
            )
        fixed[canonical] = idx

    optional: dict[str, int] = {}
    for canonical in _OPTIONAL_HEADERS:
        idx = index_by_lower.get(canonical.lower())
        if idx is not None:
            optional[canonical] = idx

    fixed_lower = {h.lower() for h in _REQUIRED_HEADERS}
    optional_lower = {h.lower() for h in _OPTIONAL_HEADERS}

    # Dynamic model columns: any *_messages header that is not a fixed/optional
    # field (Total_Messages is fixed and excluded here).
    model_by_slug: dict[str, tuple[str, int]] = {}
    for i, original in enumerate(header):
        name = original.strip()
        low = name.lower()
        if low in fixed_lower or low in optional_lower:
            continue
        if not low.endswith(_MODEL_SUFFIX):
            continue
        raw_name = name[: -len(_MODEL_SUFFIX)]
        if raw_name.strip() == "":
            # A bare "_messages" column has no model identity; treat as unknown
            # extra column and ignore it.
            continue
        slug = _slugify(raw_name)
        if slug == "":
            continue
        if slug in model_by_slug:
            raise _RowError(
                IssueCode.AMBIGUOUS_MODEL_HEADER,
                "two model columns resolved to the same name",
            )
        model_by_slug[slug] = (raw_name, i)

    models = tuple(
        (slug, raw, idx) for slug, (raw, idx) in sorted(model_by_slug.items())
    )
    return _HeaderPlan(fixed, optional, models)


def _build_record(
    fields: list[str], plan: _HeaderPlan, source: SourceRef
) -> UsageRecord:
    """Build one canonical record from a data row, or raise :class:`_RowError`."""

    def cell(canonical: str) -> str:
        return fields[plan.fixed[canonical]]

    date = _parse_date(cell("Date"))
    client_type = _parse_client_type(cell("Client_Type"))
    profile_id = _require_nonempty(cell("ProfileId"), "profile id")
    user_id = _require_nonempty(cell("UserId"), "user id")
    user_email = _require_nonempty(cell("User_Email"), "email")
    subscription_tier = _require_nonempty(cell("Subscription_Tier"), "tier")

    chat_conversations = _parse_int(cell("Chat_Conversations"))
    total_messages = _parse_int(cell("Total_Messages"))
    credits_used = _parse_decimal(cell("Credits_Used"))
    overage_cap = _parse_decimal(cell("Overage_Cap"))
    overage_credits_used = _parse_decimal(cell("Overage_Credits_Used"))
    overage_enabled = _parse_bool(cell("Overage_Enabled"))
    new_user = _parse_bool(cell("New_User"))

    usage_limit: Decimal | None = None
    if "Usage_Limit" in plan.optional:
        usage_limit = _parse_decimal(fields[plan.optional["Usage_Limit"]], allow_optional=True)

    models: list[ModelUsage] = []
    for slug, raw_name, idx in plan.models:
        count = _parse_int(fields[idx])
        if count > 0:
            models.append(ModelUsage(slug=slug, raw_name=raw_name, messages=count))
    models.sort()

    # Required Decimal fields are guaranteed non-None by _parse_decimal (it
    # only returns None when allow_optional is set, which these are not).
    if credits_used is None or overage_cap is None or overage_credits_used is None:
        raise _RowError(IssueCode.INVALID_VALUE, "a required numeric field was empty")

    return UsageRecord(
        date=date,
        client_type=client_type,
        profile_id=profile_id,
        user_id=user_id,
        user_email=user_email,
        subscription_tier=subscription_tier,
        chat_conversations=chat_conversations,
        total_messages=total_messages,
        credits_used=credits_used,
        overage_cap=overage_cap,
        overage_credits_used=overage_credits_used,
        overage_enabled=overage_enabled,
        new_user=new_user,
        models=tuple(models),
        usage_limit=usage_limit,
        source=source,
    )


class CsvParseOutput:
    """Result of parsing one CSV member: records plus safe issues."""

    __slots__ = ("records", "issues")

    def __init__(self, records: list[UsageRecord], issues: list[DataIssue]) -> None:
        self.records = records
        self.issues = issues


def parse_member_csv(
    raw_bytes: bytes,
    source: SourceRef,
    limits: CsvLimits,
    *,
    remaining_import_rows: int,
) -> CsvParseOutput:
    """Parse one CSV member into canonical records and safe issues.

    ``raw_bytes`` is decoded as UTF-8-SIG (so a BOM is stripped). Invalid UTF-8
    yields a single file-level issue and no records. Quoting and embedded
    newlines are handled by :mod:`csv`. Row and import limits are enforced
    while iterating.

    ``remaining_import_rows`` is how many more data rows the whole import can
    still accept; when exhausted an import-level limit issue is emitted and
    parsing of this member stops.
    """

    records: list[UsageRecord] = []
    issues: list[DataIssue] = []

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        issues.append(
            DataIssue(
                severity=IssueSeverity.ERROR,
                code=IssueCode.INVALID_ENCODING,
                message="The file was not valid UTF-8 and was skipped.",
                source=source,
            )
        )
        return CsvParseOutput(records, issues)

    if text.strip() == "":
        issues.append(
            DataIssue(
                severity=IssueSeverity.ERROR,
                code=IssueCode.EMPTY_FILE,
                message="The file was empty and was skipped.",
                source=source,
            )
        )
        return CsvParseOutput(records, issues)

    reader = csv.reader(io.StringIO(text, newline=""))
    row_iter: Iterator[list[str]] = iter(reader)

    try:
        header = next(row_iter)
    except StopIteration:  # pragma: no cover - guarded by empty check above
        issues.append(
            DataIssue(
                severity=IssueSeverity.ERROR,
                code=IssueCode.EMPTY_FILE,
                message="The file was empty and was skipped.",
                source=source,
            )
        )
        return CsvParseOutput(records, issues)

    try:
        plan = _resolve_headers(header)
    except _RowError as exc:
        issues.append(
            DataIssue(
                severity=IssueSeverity.ERROR,
                code=exc.code,
                message=_file_issue_message(exc.code),
                source=source,
            )
        )
        return CsvParseOutput(records, issues)

    expected_fields = len(header)
    if not plan.models:
        issues.append(
            DataIssue(
                severity=IssueSeverity.WARNING,
                code=IssueCode.NO_MODEL_COLUMNS,
                message="No per-model message columns were present in this file.",
                source=source,
            )
        )

    data_row_number = 0
    saw_data_row = False
    import_budget = remaining_import_rows

    for fields in row_iter:
        # Skip genuinely blank lines produced by trailing newlines.
        if len(fields) == 1 and fields[0] == "":
            continue
        saw_data_row = True
        data_row_number += 1

        if data_row_number > limits.max_rows_per_csv:
            issues.append(
                DataIssue(
                    severity=IssueSeverity.ERROR,
                    code=IssueCode.ROW_LIMIT_EXCEEDED,
                    message="The file exceeded the maximum rows and was truncated.",
                    source=source,
                )
            )
            break

        if import_budget <= 0:
            issues.append(
                DataIssue(
                    severity=IssueSeverity.ERROR,
                    code=IssueCode.IMPORT_ROW_LIMIT_EXCEEDED,
                    message="The import row limit was reached; remaining rows were skipped.",
                    source=source,
                )
            )
            break

        if len(fields) != expected_fields:
            issues.append(
                DataIssue(
                    severity=IssueSeverity.ERROR,
                    code=IssueCode.WRONG_FIELD_COUNT,
                    message="A row had the wrong number of fields and was skipped.",
                    source=source,
                    row=data_row_number,
                )
            )
            continue

        try:
            record = _build_record(fields, plan, source)
        except _RowError as exc:
            issues.append(
                DataIssue(
                    severity=IssueSeverity.ERROR,
                    code=exc.code,
                    message=_row_issue_message(exc.code),
                    source=source,
                    row=data_row_number,
                )
            )
            continue

        # Warn (but accept) if model-message sum differs from Total_Messages.
        model_sum = sum(m.messages for m in record.models)
        if record.models and model_sum != record.total_messages:
            issues.append(
                DataIssue(
                    severity=IssueSeverity.WARNING,
                    code=IssueCode.MESSAGE_SUM_MISMATCH,
                    message=(
                        "The sum of per-model messages did not match the total "
                        "for a row."
                    ),
                    source=source,
                    row=data_row_number,
                )
            )

        records.append(record)
        import_budget -= 1

    if not saw_data_row:
        issues.append(
            DataIssue(
                severity=IssueSeverity.WARNING,
                code=IssueCode.HEADER_ONLY,
                message="The file contained only a header row and no data.",
                source=source,
            )
        )

    return CsvParseOutput(records, issues)


def _file_issue_message(code: IssueCode) -> str:
    return {
        IssueCode.BLANK_HEADER: "A header cell was blank; the file was skipped.",
        IssueCode.DUPLICATE_HEADER: "A header name was duplicated; the file was skipped.",
        IssueCode.MISSING_REQUIRED_HEADER: (
            "A required column was missing; the file was skipped."
        ),
        IssueCode.AMBIGUOUS_MODEL_HEADER: (
            "Two model columns resolved to the same name; the file was skipped."
        ),
    }.get(code, "The file could not be parsed and was skipped.")


def _row_issue_message(code: IssueCode) -> str:
    return {
        IssueCode.INVALID_VALUE: "A row had an invalid value and was skipped.",
        IssueCode.EMPTY_IDENTITY: "A row had an empty identity field and was skipped.",
    }.get(code, "A row could not be parsed and was skipped.")


__all__ = ["parse_member_csv", "CsvParseOutput"]
