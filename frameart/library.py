"""SQLite-backed library metadata, collections, and display history."""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path


class LibraryStore:
    """Persist user-managed metadata alongside the async job database."""

    def __init__(self, data_dir: Path) -> None:
        self.database_path = Path(data_dir) / "frameart.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS library_tags (
                    job_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (job_id, tag)
                );
                CREATE INDEX IF NOT EXISTS idx_library_tags_tag ON library_tags(tag);

                CREATE TABLE IF NOT EXISTS library_collections (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_collection_items (
                    collection_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    added_at REAL NOT NULL,
                    PRIMARY KEY (collection_id, job_id),
                    FOREIGN KEY (collection_id) REFERENCES library_collections(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_collection_items_job
                    ON library_collection_items(job_id);

                CREATE TABLE IF NOT EXISTS display_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    content_id TEXT,
                    tv_target TEXT,
                    source TEXT NOT NULL,
                    displayed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_display_history_time
                    ON display_history(displayed_at DESC);
                """
            )
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def normalize_tags(tags: list[str]) -> list[str]:
        normalized = [tag.strip().lower() for tag in tags if tag.strip()]
        return list(dict.fromkeys(normalized))

    def set_tags(self, job_id: str, tags: list[str]) -> list[str]:
        normalized = self.normalize_tags(tags)
        with self._connect() as connection:
            connection.execute("DELETE FROM library_tags WHERE job_id = ?", (job_id,))
            connection.executemany(
                "INSERT INTO library_tags (job_id, tag) VALUES (?, ?)",
                [(job_id, tag) for tag in normalized],
            )
        return normalized

    def metadata_for_jobs(self, job_ids: list[str]) -> dict[str, dict[str, list[str]]]:
        metadata = {job_id: {"tags": [], "collections": []} for job_id in job_ids}
        if not job_ids:
            return metadata
        placeholders = ",".join("?" for _ in job_ids)
        with self._connect() as connection:
            tag_rows = connection.execute(
                f"""
                SELECT job_id, tag FROM library_tags
                WHERE job_id IN ({placeholders}) ORDER BY tag
                """,
                job_ids,
            ).fetchall()
            collection_rows = connection.execute(
                f"""
                SELECT items.job_id, collections.name
                FROM library_collection_items AS items
                JOIN library_collections AS collections ON collections.id = items.collection_id
                WHERE items.job_id IN ({placeholders})
                ORDER BY collections.name COLLATE NOCASE
                """,
                job_ids,
            ).fetchall()
        for row in tag_rows:
            metadata[row["job_id"]]["tags"].append(row["tag"])
        for row in collection_rows:
            metadata[row["job_id"]]["collections"].append(row["name"])
        return metadata

    def list_collections(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT collections.id, collections.name, collections.created_at,
                       COUNT(items.job_id) AS item_count
                FROM library_collections AS collections
                LEFT JOIN library_collection_items AS items
                    ON items.collection_id = collections.id
                GROUP BY collections.id
                ORDER BY collections.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_collection(self, name: str) -> dict[str, object]:
        collection = {"id": uuid.uuid4().hex, "name": name.strip(), "created_at": time.time()}
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO library_collections (id, name, created_at) VALUES (?, ?, ?)",
                (collection["id"], collection["name"], collection["created_at"]),
            )
        collection["item_count"] = 0
        return collection

    def delete_collection(self, collection_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM library_collections WHERE id = ?", (collection_id,)
            )
        return cursor.rowcount > 0

    def add_collection_items(self, collection_id: str, job_ids: list[str]) -> None:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM library_collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if not exists:
                raise KeyError(collection_id)
            connection.executemany(
                """
                INSERT OR IGNORE INTO library_collection_items (collection_id, job_id, added_at)
                VALUES (?, ?, ?)
                """,
                [(collection_id, job_id, time.time()) for job_id in job_ids],
            )

    def remove_collection_items(self, collection_id: str, job_ids: list[str]) -> None:
        with self._connect() as connection:
            connection.executemany(
                """
                DELETE FROM library_collection_items
                WHERE collection_id = ? AND job_id = ?
                """,
                [(collection_id, job_id) for job_id in job_ids],
            )

    def collection_job_ids(self, collection_id: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM library_collection_items WHERE collection_id = ?",
                (collection_id,),
            ).fetchall()
        return {row["job_id"] for row in rows}

    def remove_job(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM library_tags WHERE job_id = ?", (job_id,))
            connection.execute(
                "DELETE FROM library_collection_items WHERE job_id = ?", (job_id,)
            )

    def record_display(
        self,
        *,
        job_id: str | None,
        content_id: str | None,
        tv_target: str | None,
        source: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO display_history (
                    job_id, content_id, tv_target, source, displayed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, content_id, tv_target, source, time.time()),
            )
            connection.execute(
                """
                DELETE FROM display_history WHERE id NOT IN (
                    SELECT id FROM display_history ORDER BY displayed_at DESC LIMIT 1000
                )
                """
            )

    def list_history(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, job_id, content_id, tv_target, source, displayed_at
                FROM display_history ORDER BY displayed_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
