"""Ephemeral in-memory analysis sessions.

Sessions are keyed by an unguessable token and expire after a configurable
period of inactivity. There is no database and nothing is persisted to disk;
restarting the process clears all state.

The store is **generic** over the value it holds. The upload flow stores an
immutable :class:`~kiro_vfm.domain.usage.ImportResult`. That result may contain
personal data (user ids, emails) inside its canonical records, but it is held
**only in memory** for the session's lifetime — never written to disk, never
logged, and dropped on expiry or process restart. The store never retains ZIP
bytes, CSV bytes, streams, or raw reader rows.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Entry[T]:
    value: T
    last_access: float


class SessionStore[T]:
    """Thread-safe, time-expiring, in-memory session store."""

    def __init__(
        self, ttl_seconds: int, *, clock: Callable[[], float] | None = None
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry[T]] = {}

    def _purge_expired(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.last_access > self._ttl
        ]
        for key in expired:
            del self._entries[key]

    def create(self, value: T) -> str:
        now = self._clock()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_expired(now)
            self._entries[token] = _Entry(value=value, last_access=now)
        return token

    def get(self, token: str) -> T | None:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(token)
            if entry is None:
                return None
            entry.last_access = now
            return entry.value

    def discard(self, token: str) -> None:
        with self._lock:
            self._entries.pop(token, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
