"""Safe inspection of uploaded Kiro report ZIP archives.

Security properties:

* Members are **never** extracted to the filesystem. We inspect them purely
  through :mod:`zipfile` metadata and (for small CSV members) streamed reads.
* The ZIP signature is validated before anything else.
* Member names are rejected if they attempt path traversal, use absolute
  paths, contain backslashes or NUL bytes, or name non-regular entries
  (symlinks, devices, directories masquerading as data).
* Encrypted members are rejected (we cannot and will not decrypt).
* Archive-bomb defences enforce member count, per-member and total expanded
  size, and a per-member compression ratio ceiling.
* Member-controlled strings (names, reasons) are returned as data only; the
  web layer is responsible for HTML-escaping them. We additionally sanitise
  names for display so control characters cannot leak into logs or pages.

Nothing here logs member content or PII.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from enum import StrEnum

from ..config import ArchiveLimits

# Unix mode bits (stored in the high 16 bits of external_attr for Unix hosts).
_S_IFMT = 0o170000
_S_IFREG = 0o100000
_S_IFDIR = 0o040000
_S_IFLNK = 0o120000

# ZIP general purpose bit 0 indicates encryption.
_FLAG_ENCRYPTED = 0x1

# Metadata entries that are harmless and simply ignored.
_IGNORED_BASENAMES = frozenset({".DS_Store", "Thumbs.db"})
_IGNORED_PREFIXES = ("__MACOSX/",)

# Report filenames end with ``_user_report_YYYYMMDDHHMM[SS].csv`` and may carry
# an optional ``_part_N`` segment, placed either just before the timestamp or
# just after it (before ``.csv``). The timestamp is 12 or 14 digits.
_TS_RE = re.compile(
    r"_user_report_(?P<ts>\d{12}(?:\d{2})?)(?:_part_\d+)?\.csv$",
    re.IGNORECASE,
)
_TS_RE_PART_BEFORE = re.compile(
    r"_part_\d+_user_report_(?P<ts>\d{12}(?:\d{2})?)\.csv$",
    re.IGNORECASE,
)


class MemberStatus(StrEnum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    REJECTED = "rejected"


class ArchiveError(Exception):
    """Raised when the archive as a whole must be rejected."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class MemberReport:
    """A safe, display-ready report about a single archive member."""

    display_name: str
    status: MemberStatus
    reason: str = ""


@dataclass(frozen=True)
class ArchiveSummary:
    """Aggregate result of validating an archive. Contains no file content."""

    compressed_bytes: int
    total_members: int
    accepted_count: int
    ignored_count: int
    rejected_count: int
    csv_count: int
    members: tuple[MemberReport, ...] = ()


def _sanitise_for_display(name: str) -> str:
    """Return a name safe to store/display.

    Strips control characters (including NUL) so that neither logs nor the
    rendered page can be corrupted by a hostile member name. The web layer
    still HTML-escapes this value; this is defence in depth.
    """

    cleaned = "".join(ch if ch.isprintable() else "\ufffd" for ch in name)
    # Cap length to avoid oversized rows from pathological names.
    return cleaned[:256]


def _is_ignored(name: str) -> bool:
    if any(name.startswith(prefix) for prefix in _IGNORED_PREFIXES):
        return True
    base = name.rsplit("/", 1)[-1]
    return base in _IGNORED_BASENAMES


def _is_directory_entry(info: zipfile.ZipInfo) -> bool:
    return info.filename.endswith("/")


def _unix_mode(info: zipfile.ZipInfo) -> int:
    # Only the high 16 bits carry Unix permission/type bits.
    return (info.external_attr >> 16) & 0xFFFF


def _unsafe_name_reason(name: str) -> str | None:
    """Return a rejection reason if the member name is unsafe, else None."""

    if "\x00" in name:
        return "member name contains a NUL byte"
    if "\\" in name:
        return "member name contains a backslash"
    if name.startswith("/"):
        return "member name is an absolute path"
    # Windows drive-letter absolute path, e.g. C:\ or C:/ (backslash already
    # caught above, but a forward-slash drive path is still absolute).
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        return "member name is an absolute (drive) path"
    parts = name.split("/")
    if any(part == ".." for part in parts):
        return "member name attempts path traversal"
    return None


def _classify_member(info: zipfile.ZipInfo, limits: ArchiveLimits) -> MemberReport:
    raw_name = info.filename
    display = _sanitise_for_display(raw_name)

    # Reject unsafe names before anything else, including before the
    # ignored-metadata and directory short-circuits. Otherwise a hostile
    # member could smuggle traversal or an absolute path past validation by
    # dressing it up as metadata (e.g. "__MACOSX/../../etc/passwd") or as a
    # directory entry (e.g. "../evil/").
    name_reason = _unsafe_name_reason(raw_name)
    if name_reason is not None:
        return MemberReport(display, MemberStatus.REJECTED, name_reason)

    # Reject encrypted members early so an encrypted entry cannot be ignored.
    if info.flag_bits & _FLAG_ENCRYPTED:
        return MemberReport(display, MemberStatus.REJECTED, "member is encrypted")

    # Reject non-regular entries (symlinks, devices, fifos, ...) before the
    # ignored/directory short-circuits so a symlink or special file cannot be
    # smuggled under a metadata prefix such as "__MACOSX/". A mode of 0 means
    # the host did not record Unix bits; treat that as a regular file. We allow
    # the directory type here so genuine directory entries fall through to the
    # directory handling below.
    mode = _unix_mode(info)
    if mode != 0:
        fmt = mode & _S_IFMT
        if fmt == _S_IFLNK:
            return MemberReport(display, MemberStatus.REJECTED, "member is a symlink")
        if fmt not in (_S_IFREG, _S_IFDIR, 0):
            return MemberReport(
                display, MemberStatus.REJECTED, "member is a special (non-regular) file"
            )

    # Harmless metadata is ignored once we know the name and type are safe.
    if _is_ignored(raw_name):
        return MemberReport(display, MemberStatus.IGNORED, "metadata entry ignored")

    # Directory entries carry no data; ignore them silently.
    if _is_directory_entry(info):
        return MemberReport(display, MemberStatus.IGNORED, "directory entry")

    # Archive-bomb defences on declared sizes.
    if info.file_size > limits.max_member_expanded_bytes:
        return MemberReport(
            display, MemberStatus.REJECTED, "member exceeds expanded-size limit"
        )
    if info.compress_size > 0:
        ratio = info.file_size / info.compress_size
        if ratio > limits.max_compression_ratio:
            return MemberReport(
                display, MemberStatus.REJECTED, "member exceeds compression-ratio limit"
            )

    # Only .csv members are accepted as reports; anything else is unsupported.
    lowered = raw_name.lower()
    if lowered.endswith(".csv"):
        return MemberReport(display, MemberStatus.ACCEPTED, "CSV report")
    return MemberReport(display, MemberStatus.REJECTED, "unsupported member type")


def validate_archive(data: bytes, limits: ArchiveLimits) -> ArchiveSummary:
    """Validate an uploaded archive held in memory.

    Raises :class:`ArchiveError` for whole-archive failures (bad signature,
    over-limit, no CSV reports, etc.). Returns an :class:`ArchiveSummary`
    describing per-member outcomes on success.

    ``data`` is treated as untrusted and is never written to disk.
    """

    compressed_bytes = len(data)
    if compressed_bytes == 0:
        raise ArchiveError("uploaded file is empty")
    if compressed_bytes > limits.max_compressed_bytes:
        raise ArchiveError("archive exceeds the maximum allowed upload size")

    # Validate the ZIP signature / central directory before trusting anything.
    buffer = io.BytesIO(data)
    if not zipfile.is_zipfile(buffer):
        raise ArchiveError("uploaded file is not a valid ZIP archive")
    buffer.seek(0)

    try:
        with zipfile.ZipFile(buffer) as zf:
            # testzip would decompress every member; instead we rely on
            # metadata plus targeted limits so we never expand bombs.
            infos = zf.infolist()

            if len(infos) > limits.max_member_count:
                raise ArchiveError("archive contains too many members")

            members: list[MemberReport] = []
            total_expanded = 0
            accepted = ignored = rejected = csv_count = 0

            for info in infos:
                report = _classify_member(info, limits)
                members.append(report)

                if report.status is MemberStatus.ACCEPTED:
                    accepted += 1
                    csv_count += 1
                    # Count declared expanded size toward the total budget.
                    total_expanded += info.file_size
                    if total_expanded > limits.max_total_expanded_bytes:
                        raise ArchiveError(
                            "archive exceeds the total expanded-size limit"
                        )
                elif report.status is MemberStatus.IGNORED:
                    ignored += 1
                else:
                    rejected += 1

    except ArchiveError:
        raise
    except zipfile.BadZipFile as exc:  # pragma: no cover - defensive
        raise ArchiveError("archive is malformed") from exc

    if csv_count == 0:
        raise ArchiveError("archive contains no CSV reports")

    return ArchiveSummary(
        compressed_bytes=compressed_bytes,
        total_members=len(infos),
        accepted_count=accepted,
        ignored_count=ignored,
        rejected_count=rejected,
        csv_count=csv_count,
        members=tuple(members),
    )


# ---------------------------------------------------------------------------
# Safe per-member reading (for Task 2 CSV parsing).
#
# We never extract to disk and never call ``ZipFile.extractall`` /
# ``ZipFile.extract``. Each accepted member is read via ``ZipFile.open`` in a
# streaming loop with a hard upper bound on the number of *actual*
# decompressed bytes read, so a member whose metadata understates its true
# size cannot exhaust memory. Only one member's bytes are held at a time and
# they are released by the caller after parsing.
# ---------------------------------------------------------------------------


class MemberReadError(Exception):
    """Raised when a single accepted member cannot be safely read.

    Carries only a generic, safe reason — never member content.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_report_timestamp(name: str) -> str | None:
    """Return the report timestamp digits from a member name, or ``None``.

    Accepts both 12-digit (``YYYYMMDDHHMM``) and 14-digit
    (``YYYYMMDDHHMMSS``) stamps and an optional ``_part_N`` segment placed
    either before or after the ``_user_report_<ts>`` portion. The returned
    value is a digit string suitable for lexicographic comparison within a
    fixed width; callers normalise width before comparing. Not PII.
    """

    base = name.rsplit("/", 1)[-1]
    match = _TS_RE.search(base) or _TS_RE_PART_BEFORE.search(base)
    if match is None:
        return None
    return match.group("ts")


def read_member_bounded(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int
) -> bytes:
    """Read one member's bytes with a hard actual-byte upper bound.

    Reads in fixed-size chunks and stops if the cumulative decompressed size
    would exceed ``max_bytes`` even by one byte, regardless of what the ZIP
    metadata claims. CRC, malformed, unsupported-compression and other read
    errors are surfaced as :class:`MemberReadError` with a generic reason.
    """

    chunk_size = 64 * 1024
    collected = bytearray()
    try:
        with zf.open(info, "r") as handle:
            while True:
                remaining = max_bytes - len(collected)
                if remaining <= 0:
                    # Read one more byte to detect over-limit members.
                    if handle.read(1):
                        raise MemberReadError("member exceeds the read-size limit")
                    break
                chunk = handle.read(min(chunk_size, remaining))
                if not chunk:
                    break
                collected.extend(chunk)
    except MemberReadError:
        raise
    except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError, ValueError) as exc:
        raise MemberReadError("member could not be decompressed") from exc
    return bytes(collected)


def iter_accepted_csv_members(
    data: bytes, limits: ArchiveLimits
) -> list[tuple[int, str, zipfile.ZipInfo]]:
    """Return accepted CSV members as ``(index, display_name, ZipInfo)``.

    Classification reuses the same rules as :func:`validate_archive` so only
    Task-1 ACCEPTED CSV entries are returned. The list preserves archive order
    and assigns each a stable 0-based index for deterministic provenance.
    """

    buffer = io.BytesIO(data)
    results: list[tuple[int, str, zipfile.ZipInfo]] = []
    with zipfile.ZipFile(buffer) as zf:
        for index, info in enumerate(zf.infolist()):
            report = _classify_member(info, limits)
            if report.status is MemberStatus.ACCEPTED:
                results.append((index, report.display_name, info))
    return results
