"""Persistent, bounded caches for slow Samsung TV metadata and thumbnails."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frameart.config import TVProfile

MATTE_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_THUMBNAIL_ENTRIES = 500
MAX_THUMBNAIL_BYTES = 256 * 1024 * 1024
THUMBNAIL_FETCH_SLOTS_PER_TV = 2

_KEY_LOCKS: dict[str, threading.Lock] = {}
_THUMBNAIL_SLOTS: dict[str, threading.BoundedSemaphore] = {}
_REGISTRY_GUARD = threading.Lock()


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    updated_at: float


def tv_cache_key(profile: TVProfile) -> str:
    """Return a stable cache key for one physical TV endpoint."""
    return f"{profile.ip}:{profile.port}"


def _key_lock(key: str) -> threading.Lock:
    with _REGISTRY_GUARD:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock


def _thumbnail_slots(tv_key: str) -> threading.BoundedSemaphore:
    with _REGISTRY_GUARD:
        slots = _THUMBNAIL_SLOTS.get(tv_key)
        if slots is None:
            slots = threading.BoundedSemaphore(THUMBNAIL_FETCH_SLOTS_PER_TV)
            _THUMBNAIL_SLOTS[tv_key] = slots
        return slots


@contextmanager
def coalesced_cache_fill(kind: str, tv_key: str, item_id: str = "") -> Iterator[None]:
    """Coalesce concurrent cache misses for the same logical cache entry."""
    lock = _key_lock(f"{kind}:{tv_key}:{item_id}")
    with lock:
        yield


@contextmanager
def thumbnail_fetch_slot(tv_key: str, timeout_sec: float = 0.05) -> Iterator[bool]:
    """Bound the number of cold thumbnail requests admitted for one TV."""
    slots = _thumbnail_slots(tv_key)
    acquired = slots.acquire(timeout=max(0.0, timeout_sec))
    try:
        yield acquired
    finally:
        if acquired:
            slots.release()


class TVCacheStore:
    """Persist matte metadata in SQLite and thumbnails as owner-private files."""

    def __init__(
        self,
        data_dir: Path,
        *,
        max_thumbnail_entries: int = MAX_THUMBNAIL_ENTRIES,
        max_thumbnail_bytes: int = MAX_THUMBNAIL_BYTES,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.database_path = self.data_dir / "frameart.sqlite3"
        self.thumbnail_dir = self.data_dir / "tv_cache" / "thumbnails"
        self.max_thumbnail_entries = max(1, max_thumbnail_entries)
        self.max_thumbnail_bytes = max(1, max_thumbnail_bytes)
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tv_matte_cache (
                    tv_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tv_thumbnail_cache (
                    tv_key TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (tv_key, content_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tv_thumbnail_cache_updated
                    ON tv_thumbnail_cache(updated_at);
                """
            )
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def get_mattes(self, tv_key: str) -> CacheEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json, updated_at FROM tv_matte_cache WHERE tv_key = ?",
                (tv_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value_json"])
        except (TypeError, ValueError):
            self.invalidate_mattes(tv_key)
            return None
        if not isinstance(value, list) or not value:
            self.invalidate_mattes(tv_key)
            return None
        return CacheEntry(value=value, updated_at=float(row["updated_at"]))

    def set_mattes(self, tv_key: str, mattes: list[Any]) -> CacheEntry | None:
        if not mattes:
            return None
        updated_at = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tv_matte_cache (tv_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tv_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (tv_key, json.dumps(mattes), updated_at),
            )
        return CacheEntry(value=mattes, updated_at=updated_at)

    def invalidate_mattes(self, tv_key: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM tv_matte_cache WHERE tv_key = ?", (tv_key,))

    @staticmethod
    def _thumbnail_filename(tv_key: str, content_id: str) -> str:
        digest = hashlib.sha256(f"{tv_key}\0{content_id}".encode()).hexdigest()
        return f"{digest}.jpg"

    def get_thumbnail(self, tv_key: str, content_id: str) -> CacheEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT filename, media_type, updated_at
                FROM tv_thumbnail_cache WHERE tv_key = ? AND content_id = ?
                """,
                (tv_key, content_id),
            ).fetchone()
        if row is None:
            return None
        path = self.thumbnail_dir / row["filename"]
        try:
            value = {"data": path.read_bytes(), "media_type": row["media_type"]}
        except OSError:
            self.delete_thumbnails(tv_key, [content_id])
            return None
        return CacheEntry(value=value, updated_at=float(row["updated_at"]))

    def set_thumbnail(
        self,
        tv_key: str,
        content_id: str,
        data: bytes,
        media_type: str = "image/jpeg",
    ) -> CacheEntry | None:
        if not data:
            return None
        filename = self._thumbnail_filename(tv_key, content_id)
        destination = self.thumbnail_dir / filename
        with tempfile.NamedTemporaryFile(dir=self.thumbnail_dir, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

        updated_at = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tv_thumbnail_cache (
                    tv_key, content_id, filename, media_type, size_bytes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tv_key, content_id) DO UPDATE SET
                    filename = excluded.filename,
                    media_type = excluded.media_type,
                    size_bytes = excluded.size_bytes,
                    updated_at = excluded.updated_at
                """,
                (tv_key, content_id, filename, media_type, len(data), updated_at),
            )
        self._evict_thumbnails()
        return CacheEntry(
            value={"data": bytes(data), "media_type": media_type},
            updated_at=updated_at,
        )

    def delete_thumbnails(self, tv_key: str, content_ids: list[str]) -> None:
        if not content_ids:
            return
        placeholders = ",".join("?" for _ in content_ids)
        parameters = [tv_key, *content_ids]
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT filename FROM tv_thumbnail_cache
                WHERE tv_key = ? AND content_id IN ({placeholders})
                """,  # noqa: S608
                parameters,
            ).fetchall()
            connection.execute(
                f"""
                DELETE FROM tv_thumbnail_cache
                WHERE tv_key = ? AND content_id IN ({placeholders})
                """,  # noqa: S608
                parameters,
            )
        for row in rows:
            (self.thumbnail_dir / row["filename"]).unlink(missing_ok=True)

    def _evict_thumbnails(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT tv_key, content_id, filename, size_bytes
                FROM tv_thumbnail_cache ORDER BY updated_at DESC
                """
            ).fetchall()
            kept_bytes = 0
            stale: list[sqlite3.Row] = []
            for index, row in enumerate(rows):
                kept_bytes += int(row["size_bytes"])
                if index >= self.max_thumbnail_entries or kept_bytes > self.max_thumbnail_bytes:
                    stale.append(row)
            if stale:
                connection.executemany(
                    "DELETE FROM tv_thumbnail_cache WHERE tv_key = ? AND content_id = ?",
                    [(row["tv_key"], row["content_id"]) for row in stale],
                )
        for row in stale:
            (self.thumbnail_dir / row["filename"]).unlink(missing_ok=True)
