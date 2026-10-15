"""Unit tests for the safe archive validator."""

from __future__ import annotations

import pytest

from kiro_vfm.adapters.archive import (
    ArchiveError,
    MemberStatus,
    validate_archive,
)
from kiro_vfm.config import ArchiveLimits
from tests.helpers import archive_factory as af

LIMITS = ArchiveLimits()


def _reasons(summary, status: MemberStatus) -> list[str]:
    return [m.reason for m in summary.members if m.status is status]


def test_valid_nested_archive_is_accepted() -> None:
    data = af.make_zip(
        [
            ("reports/2026-01-01/KIRO_CLI.csv", b"Date,Credits_Used\n2026-01-01,1\n"),
            ("reports/2026-01-01/KIRO_IDE.csv", b"Date,Credits_Used\n2026-01-01,2\n"),
        ]
    )
    summary = validate_archive(data, LIMITS)
    assert summary.csv_count == 2
    assert summary.accepted_count == 2
    assert summary.rejected_count == 0


def test_ignores_harmless_metadata() -> None:
    data = af.make_zip(
        [
            ("reports/day.csv", b"col\n1\n"),
            ("__MACOSX/._day.csv", b"junk"),
            ("reports/.DS_Store", b"junk"),
            ("Thumbs.db", b"junk"),
        ]
    )
    summary = validate_archive(data, LIMITS)
    assert summary.csv_count == 1
    assert summary.ignored_count == 3


def test_empty_upload_rejected() -> None:
    with pytest.raises(ArchiveError, match="empty"):
        validate_archive(b"", LIMITS)


def test_non_zip_rejected() -> None:
    with pytest.raises(ArchiveError, match="not a valid ZIP"):
        validate_archive(b"this is definitely not a zip file", LIMITS)


def test_truncated_zip_rejected() -> None:
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    with pytest.raises(ArchiveError):
        validate_archive(data[: len(data) // 2], LIMITS)


def test_no_csv_rejected() -> None:
    data = af.make_zip([("readme.txt", b"hello"), ("notes.md", b"world")])
    with pytest.raises(ArchiveError, match="no CSV reports"):
        validate_archive(data, LIMITS)


def test_unsupported_only_archive_rejected() -> None:
    data = af.make_zip([("data.json", b"{}"), ("image.png", b"\x89PNG")])
    with pytest.raises(ArchiveError, match="no CSV reports"):
        validate_archive(data, LIMITS)


def test_over_compressed_size_rejected() -> None:
    small = ArchiveLimits(max_compressed_bytes=10)
    data = af.make_zip([("reports/day.csv", b"col\n1\n")])
    with pytest.raises(ArchiveError, match="maximum allowed upload size"):
        validate_archive(data, small)


def test_too_many_members_rejected() -> None:
    limits = ArchiveLimits(max_member_count=3)
    data = af.make_many_member_zip(5)
    with pytest.raises(ArchiveError, match="too many members"):
        validate_archive(data, limits)


def test_traversal_name_rejected() -> None:
    data = af.make_zip(
        [("reports/day.csv", b"col\n1\n"), ("../../etc/passwd.csv", b"x")]
    )
    summary = validate_archive(data, LIMITS)
    assert any("traversal" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_traversal_directory_entry_rejected() -> None:
    # A directory-style entry ("../evil/") must be rejected for traversal and
    # must NOT be silently ignored by the directory short-circuit.
    data = af.make_zip([("reports/day.csv", b"col\n1\n"), ("../evil/", b"")])
    summary = validate_archive(data, LIMITS)
    assert any("traversal" in r for r in _reasons(summary, MemberStatus.REJECTED))
    # It must not have been counted as an ignored directory entry.
    assert all(
        m.reason != "directory entry"
        for m in summary.members
        if m.display_name == "../evil/"
    )


def test_macosx_prefixed_traversal_rejected() -> None:
    # A member dressed up as macOS metadata must still be rejected if its name
    # attempts traversal, rather than being ignored by the metadata prefix.
    data = af.make_zip(
        [
            ("reports/day.csv", b"col\n1\n"),
            ("__MACOSX/../../etc/passwd", b"x"),
        ]
    )
    summary = validate_archive(data, LIMITS)
    assert any("traversal" in r for r in _reasons(summary, MemberStatus.REJECTED))
    assert all(
        m.status is not MemberStatus.IGNORED or "metadata" not in m.reason
        for m in summary.members
        if m.display_name == "__MACOSX/../../etc/passwd"
    )


def test_macosx_prefixed_symlink_rejected() -> None:
    # A symlink smuggled under the __MACOSX metadata prefix must be rejected,
    # not ignored.
    data = af.make_zip_with_symlink("__MACOSX/evil-link", "/etc/passwd")
    summary = validate_archive(data, LIMITS)
    assert any("symlink" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_macosx_prefixed_special_rejected() -> None:
    # A special (non-regular) file smuggled under __MACOSX must be rejected,
    # not ignored.
    data = af.make_zip_with_special("__MACOSX/evil-fifo")
    summary = validate_archive(data, LIMITS)
    assert any("special" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_absolute_name_rejected() -> None:
    data = af.make_zip([("reports/day.csv", b"col\n1\n"), ("/etc/evil.csv", b"x")])
    summary = validate_archive(data, LIMITS)
    assert any("absolute" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_windows_drive_path_rejected() -> None:
    data = af.make_zip([("reports/day.csv", b"col\n1\n"), ("C:/windows/evil.csv", b"x")])
    summary = validate_archive(data, LIMITS)
    assert any("drive" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_backslash_name_rejected() -> None:
    data = af.make_zip(
        [("reports/day.csv", b"col\n1\n"), ("reports\\win\\evil.csv", b"x")]
    )
    summary = validate_archive(data, LIMITS)
    assert any("backslash" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_nul_byte_name_rejected() -> None:
    # Python's zipfile truncates member names at a NUL on read, so a NUL in a
    # stored name is not representable through the zipfile reader. We therefore
    # assert the name guard rejects a NUL directly (defence in depth) and that
    # a crafted raw name containing a NUL would be rejected if it reached us.
    from kiro_vfm.adapters.archive import _unsafe_name_reason

    reason = _unsafe_name_reason("evil\x00.csv")
    assert reason is not None
    assert "NUL" in reason


def test_symlink_rejected() -> None:
    data = af.make_zip_with_symlink("reports/link.csv", "/etc/passwd")
    summary = validate_archive(data, LIMITS)
    assert any("symlink" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_special_file_rejected() -> None:
    data = af.make_zip_with_special("reports/fifo.csv")
    summary = validate_archive(data, LIMITS)
    assert any("special" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_encrypted_member_rejected() -> None:
    # First member is encrypted; a second, clean CSV keeps the archive valid
    # so we can inspect the per-member rejection rather than a whole-archive
    # "no CSV reports" error.
    base = af.make_zip(
        [("reports/secret.csv", b"col\n1\n"), ("reports/clean.csv", b"col\n2\n")]
    )
    data = af.mark_first_member_encrypted(base)
    summary = validate_archive(data, LIMITS)
    assert any("encrypted" in r for r in _reasons(summary, MemberStatus.REJECTED))
    assert summary.csv_count == 1


def test_member_expanded_size_limit() -> None:
    limits = ArchiveLimits(max_member_expanded_bytes=1024)
    data = af.make_highly_compressible_zip("reports/big.csv", 4096)
    summary = validate_archive(data, limits)
    assert any("expanded-size" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_compression_ratio_limit() -> None:
    limits = ArchiveLimits(max_compression_ratio=5, max_member_expanded_bytes=10_000_000)
    data = af.make_highly_compressible_zip("reports/bomb.csv", 1_000_000)
    summary = validate_archive(data, limits)
    assert any("compression-ratio" in r for r in _reasons(summary, MemberStatus.REJECTED))


def test_total_expanded_size_limit() -> None:
    limits = ArchiveLimits(
        max_total_expanded_bytes=2048,
        max_member_expanded_bytes=10_000_000,
        max_compression_ratio=1_000_000,
    )
    data = af.make_zip(
        [
            ("reports/a.csv", b"x" * 1500),
            ("reports/b.csv", b"y" * 1500),
        ]
    )
    with pytest.raises(ArchiveError, match="total expanded-size"):
        validate_archive(data, limits)


def test_exactly_at_member_count_limit_accepted() -> None:
    limits = ArchiveLimits(max_member_count=3)
    data = af.make_many_member_zip(3)
    summary = validate_archive(data, limits)
    assert summary.csv_count == 3


def test_display_name_sanitised() -> None:
    # A control character in the name must not survive into display output.
    data = af.make_zip([("reports/day.csv", b"col\n1\n"), ("bad\x01name.txt", b"x")])
    summary = validate_archive(data, LIMITS)
    assert all("\x01" not in m.display_name for m in summary.members)
