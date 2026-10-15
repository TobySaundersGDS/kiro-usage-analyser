"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kiro_vfm.app import create_app
from kiro_vfm.config import ArchiveLimits, Settings


def build_settings(**limit_overrides: int) -> Settings:
    base = ArchiveLimits()
    limits = ArchiveLimits(
        max_compressed_bytes=limit_overrides.get(
            "max_compressed_bytes", base.max_compressed_bytes
        ),
        max_member_count=limit_overrides.get("max_member_count", base.max_member_count),
        max_member_expanded_bytes=limit_overrides.get(
            "max_member_expanded_bytes", base.max_member_expanded_bytes
        ),
        max_total_expanded_bytes=limit_overrides.get(
            "max_total_expanded_bytes", base.max_total_expanded_bytes
        ),
        max_compression_ratio=limit_overrides.get(
            "max_compression_ratio", base.max_compression_ratio
        ),
    )
    return Settings(limits=limits, session_ttl_seconds=1800)


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    return TestClient(app)
