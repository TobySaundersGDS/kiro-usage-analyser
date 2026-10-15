"""View models for the recorded-usage (Level 1) page.

These are thin, display-ready shapes derived from the pure
:mod:`kiro_vfm.services.recorded_analysis` output. They:

* format :class:`~decimal.Decimal` quantities as exact, human-readable
  strings (no rounding, no float);
* expose filter state and the available filter options for the form controls;
* parse untrusted query parameters into a validated
  :class:`~kiro_vfm.services.recorded_analysis.UsageFilter`.

No estimation, pricing, or token values are produced here — only recorded
facts formatted for display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from ..domain.usage import ClientType
from ..services.recorded_analysis import RecordedAnalysis, UsageFilter

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def format_decimal(value: Decimal) -> str:
    """Format a Decimal exactly, trimming trailing fractional zeros.

    ``Decimal('1.50')`` -> ``"1.5"``; ``Decimal('2')`` -> ``"2"``;
    ``Decimal('0')`` -> ``"0"``. The value is never rounded.
    """

    normalised = value.normalize()
    # ``normalize`` can yield scientific notation (e.g. 1E+2); expand it.
    text = format(normalised, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_filter(
    *,
    date_from: str | None,
    date_to: str | None,
    client: str | None,
) -> UsageFilter:
    """Parse untrusted query params into a :class:`UsageFilter`.

    Invalid values are ignored (treated as "no bound") rather than raising, so
    a malformed query string degrades to showing everything instead of an
    error. Only well-formed ISO dates and known client types are honoured.
    """

    def clean_date(value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value if _ISO_DATE_RE.match(value) else None

    client_type: ClientType | None = None
    if client is not None and client.strip() != "":
        try:
            client_type = ClientType(client.strip())
        except ValueError:
            client_type = None

    return UsageFilter(
        date_from=clean_date(date_from),
        date_to=clean_date(date_to),
        client_type=client_type,
    )


@dataclass(frozen=True, slots=True)
class FilterView:
    """Display state for the filter form."""

    date_from: str
    date_to: str
    selected_client: str
    available_clients: tuple[str, ...]
    is_active: bool
    min_date: str
    max_date: str


def build_filter_view(analysis: RecordedAnalysis) -> FilterView:
    f = analysis.applied_filter
    return FilterView(
        date_from=f.date_from or "",
        date_to=f.date_to or "",
        selected_client=f.client_type.value if f.client_type else "",
        available_clients=tuple(c.value for c in analysis.available_clients),
        is_active=f.is_active,
        min_date=analysis.available_dates[0] if analysis.available_dates else "",
        max_date=analysis.available_dates[-1] if analysis.available_dates else "",
    )


__all__ = ["format_decimal", "parse_filter", "FilterView", "build_filter_view"]
