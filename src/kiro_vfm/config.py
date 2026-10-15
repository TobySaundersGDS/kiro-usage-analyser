"""Application configuration.

All values are plain, deterministic settings with safe defaults. The archive
limits are intentionally configurable so that tests can exercise the hostile
archive paths with small, fast fixtures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw


@dataclass(frozen=True)
class ArchiveLimits:
    """Limits used to defend against archive bombs and member abuse."""

    # Maximum size of the uploaded compressed archive in bytes (default 100 MB).
    max_compressed_bytes: int = 100 * 1024 * 1024
    # Maximum number of entries in the archive.
    max_member_count: int = 10_000
    # Maximum uncompressed size of a single member in bytes (default 50 MB).
    max_member_expanded_bytes: int = 50 * 1024 * 1024
    # Maximum total uncompressed size across all members (default 500 MB).
    max_total_expanded_bytes: int = 500 * 1024 * 1024
    # Maximum allowed compression ratio (expanded / compressed) per member.
    max_compression_ratio: int = 200


@dataclass(frozen=True)
class CsvLimits:
    """Limits applied while parsing CSV members.

    Defaults are proportionate to Kiro's daily reports (one row per user per
    client, up to ~1000 users per report part) while remaining small enough
    for the agreed modest scale. All values must be positive; non-positive
    environment overrides fall back to the default.
    """

    # Maximum number of data rows accepted from a single CSV member.
    max_rows_per_csv: int = 100_000
    # Maximum number of data rows accepted across the whole import.
    max_rows_per_import: int = 1_000_000
    # Hard upper bound on the actual decompressed bytes read from one member,
    # enforced during streaming even if the ZIP metadata understates the size.
    max_member_read_bytes: int = 50 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the local application."""

    host: str = "127.0.0.1"
    port: int = 8000
    # Inactivity expiry for in-memory analysis sessions, in seconds.
    session_ttl_seconds: int = 30 * 60
    limits: ArchiveLimits = field(default_factory=ArchiveLimits)
    csv_limits: CsvLimits = field(default_factory=CsvLimits)

    @staticmethod
    def from_env() -> Settings:
        limits = ArchiveLimits(
            max_compressed_bytes=_env_int(
                "KIRO_VFM_MAX_COMPRESSED_BYTES", ArchiveLimits().max_compressed_bytes
            ),
            max_member_count=_env_int(
                "KIRO_VFM_MAX_MEMBER_COUNT", ArchiveLimits().max_member_count
            ),
            max_member_expanded_bytes=_env_int(
                "KIRO_VFM_MAX_MEMBER_EXPANDED_BYTES",
                ArchiveLimits().max_member_expanded_bytes,
            ),
            max_total_expanded_bytes=_env_int(
                "KIRO_VFM_MAX_TOTAL_EXPANDED_BYTES",
                ArchiveLimits().max_total_expanded_bytes,
            ),
            max_compression_ratio=_env_int(
                "KIRO_VFM_MAX_COMPRESSION_RATIO", ArchiveLimits().max_compression_ratio
            ),
        )
        return Settings(
            host=_env_str("KIRO_VFM_HOST", "127.0.0.1"),
            port=_env_int("KIRO_VFM_PORT", 8000),
            session_ttl_seconds=_env_int("KIRO_VFM_SESSION_TTL_SECONDS", 30 * 60),
            limits=limits,
            csv_limits=CsvLimits(
                max_rows_per_csv=_env_int(
                    "KIRO_VFM_MAX_ROWS_PER_CSV", CsvLimits().max_rows_per_csv
                ),
                max_rows_per_import=_env_int(
                    "KIRO_VFM_MAX_ROWS_PER_IMPORT", CsvLimits().max_rows_per_import
                ),
                max_member_read_bytes=_env_int(
                    "KIRO_VFM_MAX_MEMBER_READ_BYTES", CsvLimits().max_member_read_bytes
                ),
            ),
        )
