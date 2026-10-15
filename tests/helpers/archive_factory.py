"""Helpers for building synthetic ZIP archives in tests.

No production data is ever used. Every archive here is constructed from
inert, synthetic content so that hostile-archive paths can be exercised
safely and deterministically.
"""

from __future__ import annotations

import csv
import io
import struct
import zipfile

# Unix type bits for building external_attr values.
S_IFREG = 0o100000
S_IFLNK = 0o120000
S_IFIFO = 0o010000


def _external_attr(mode: int) -> int:
    return (mode & 0xFFFF) << 16


def make_zip(members: list[tuple[str, bytes]]) -> bytes:
    """Build a simple ZIP from (name, content) pairs with regular-file modes."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members:
            info = zipfile.ZipInfo(name)
            info.external_attr = _external_attr(S_IFREG | 0o644)
            zf.writestr(info, content)
    return buffer.getvalue()


def make_zip_with_mode(name: str, content: bytes, mode: int) -> bytes:
    """Build a ZIP containing a single member with an explicit Unix mode."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo(name)
        info.external_attr = _external_attr(mode)
        zf.writestr(info, content)
    # ensure at least one valid CSV so whole-archive checks reach the member
    return buffer.getvalue()


def make_zip_with_symlink(link_name: str, target: str, *, with_csv: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if with_csv:
            good = zipfile.ZipInfo("reports/day.csv")
            good.external_attr = _external_attr(S_IFREG | 0o644)
            zf.writestr(good, b"col\n1\n")
        info = zipfile.ZipInfo(link_name)
        info.external_attr = _external_attr(S_IFLNK | 0o777)
        zf.writestr(info, target.encode())
    return buffer.getvalue()


def make_zip_with_special(name: str, *, with_csv: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if with_csv:
            good = zipfile.ZipInfo("reports/day.csv")
            good.external_attr = _external_attr(S_IFREG | 0o644)
            zf.writestr(good, b"col\n1\n")
        info = zipfile.ZipInfo(name)
        info.external_attr = _external_attr(S_IFIFO | 0o644)
        zf.writestr(info, b"")
    return buffer.getvalue()


def make_highly_compressible_zip(
    name: str, uncompressed_size: int, *, with_csv: bool = True
) -> bytes:
    """Build a ZIP whose member expands to a large, highly compressible blob."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if with_csv:
            good = zipfile.ZipInfo("reports/day.csv")
            good.external_attr = _external_attr(S_IFREG | 0o644)
            zf.writestr(good, b"col\n1\n")
        info = zipfile.ZipInfo(name)
        info.external_attr = _external_attr(S_IFREG | 0o644)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, b"\x00" * uncompressed_size)
    return buffer.getvalue()


def mark_first_member_encrypted(zip_bytes: bytes) -> bytes:
    """Flip the 'encrypted' general-purpose bit on the first member.

    This produces an archive that *declares* encryption without us needing a
    real password, which is sufficient to exercise the rejection path. The bit
    must be flipped in both the local file header (signature ``PK\\x03\\x04``,
    flags at offset 6) and the central directory header (signature
    ``PK\\x01\\x02``, flags at offset 8), because :mod:`zipfile` reads
    ``flag_bits`` from the central directory.
    """

    data = bytearray(zip_bytes)

    local = data.find(b"PK\x03\x04")
    if local < 0:
        raise ValueError("no local file header found")
    _set_flag(data, local + 6)

    central = data.find(b"PK\x01\x02")
    if central < 0:
        raise ValueError("no central directory header found")
    _set_flag(data, central + 8)

    return bytes(data)


def _set_flag(data: bytearray, offset: int) -> None:
    (flags,) = struct.unpack_from("<H", data, offset)
    flags |= 0x1
    struct.pack_into("<H", data, offset, flags)


def make_many_member_zip(count: int) -> bytes:
    members = [(f"reports/day_{i}.csv", b"col\n1\n") for i in range(count)]
    return make_zip(members)


# ---------------------------------------------------------------------------
# Synthetic Kiro report helpers for Task 2.
#
# All identities are synthetic and use the reserved example.test domain. No
# production data is referenced.
# ---------------------------------------------------------------------------

# Canonical fixed header order used by the synthetic fixtures. The parser
# resolves headers case-insensitively and tolerates reordering, so tests may
# shuffle or re-case these freely.
FIXED_HEADERS: list[str] = [
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
]


def report_row(
    *,
    date: str = "2026-01-01",
    user_id: str = "user-1",
    client_type: str = "KIRO_CLI",
    chat_conversations: str = "2",
    credits_used: str = "1.5",
    overage_cap: str = "0",
    overage_credits_used: str = "0",
    overage_enabled: str = "false",
    profile_id: str = "profile-1",
    subscription_tier: str = "Pro",
    total_messages: str = "10",
    new_user: str = "false",
    user_email: str = "person1@example.test",
) -> dict[str, str]:
    """Return a dict of fixed-field values for one synthetic report row."""

    return {
        "Date": date,
        "UserId": user_id,
        "Client_Type": client_type,
        "Chat_Conversations": chat_conversations,
        "Credits_Used": credits_used,
        "Overage_Cap": overage_cap,
        "Overage_Credits_Used": overage_credits_used,
        "Overage_Enabled": overage_enabled,
        "ProfileId": profile_id,
        "Subscription_Tier": subscription_tier,
        "Total_Messages": total_messages,
        "New_User": new_user,
        "User_Email": user_email,
    }


def build_report_csv(
    rows: list[dict[str, str]],
    *,
    model_headers: list[str] | None = None,
    extra_headers: list[str] | None = None,
    optional_usage_limit: bool = False,
    header_order: list[str] | None = None,
    encoding: str = "utf-8",
    add_bom: bool = False,
) -> bytes:
    """Build a report CSV as bytes using :mod:`csv` for correct quoting.

    ``model_headers`` are full ``<Model>_messages`` column names. ``rows``
    supply any extra values (model/extra/usage-limit) keyed by their header.
    """

    model_headers = model_headers or []
    extra_headers = extra_headers or []

    headers = list(FIXED_HEADERS)
    if optional_usage_limit:
        headers.append("Usage_Limit")
    headers.extend(model_headers)
    headers.extend(extra_headers)
    if header_order is not None:
        headers = header_order

    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(headers)
    for row in rows:
        lowered = {k.lower(): v for k, v in row.items()}
        writer.writerow([lowered.get(h.lower(), "0") for h in headers])

    text = out.getvalue()
    data = text.encode(encoding)
    if add_bom:
        data = b"\xef\xbb\xbf" + data
    return data
