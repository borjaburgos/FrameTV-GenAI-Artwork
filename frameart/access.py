"""Persistent browser-device sessions and short-lived pairing codes."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

_PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_KNOWN_SCOPES = {"read", "control", "admin"}


class InvalidPairingCodeError(ValueError):
    """Raised when a pairing code is unknown, expired, or already consumed."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_code(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


class AccessStore:
    """Store revocable device credentials without persisting raw secrets."""

    def __init__(self, data_dir: Path) -> None:
        self.database_path = Path(data_dir) / "frameart.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS access_pairings (
                    id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL UNIQUE,
                    created_by TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_access_pairings_expiry
                    ON access_pairings(expires_at);

                CREATE TABLE IF NOT EXISTS access_devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    scopes_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_access_devices_expiry
                    ON access_devices(expires_at);
                """
            )
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _device_from_row(row: sqlite3.Row) -> dict[str, object]:
        scopes = set(json.loads(row["scopes_json"])) & _KNOWN_SCOPES
        return {
            "id": row["id"],
            "name": row["name"],
            "scopes": sorted(scopes),
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
            "expires_at": row["expires_at"],
        }

    def create_pairing(self, *, created_by: str | None, lifetime_seconds: int) -> dict[str, object]:
        """Create a one-time human-readable pairing code."""
        now = time.time()
        expires_at = now + lifetime_seconds
        with self._connect() as connection:
            connection.execute("DELETE FROM access_pairings WHERE expires_at <= ?", (now,))
            for _attempt in range(5):
                raw_code = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(10))
                try:
                    pairing_id = uuid.uuid4().hex
                    connection.execute(
                        """
                        INSERT INTO access_pairings (
                            id, code_hash, created_by, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (pairing_id, _digest(raw_code), created_by, now, expires_at),
                    )
                    return {
                        "id": pairing_id,
                        "code": f"{raw_code[:5]}-{raw_code[5:]}",
                        "expires_at": expires_at,
                    }
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("Could not allocate a unique pairing code.")

    def consume_pairing(
        self,
        code: str,
        *,
        device_name: str,
        lifetime_seconds: int,
    ) -> tuple[str, dict[str, object]]:
        """Consume a pairing code and return a raw device token exactly once."""
        normalized = _normalize_code(code)
        if len(normalized) != 10:
            raise InvalidPairingCodeError("Pairing code is invalid or expired.")

        now = time.time()
        token = secrets.token_urlsafe(32)
        device_id = uuid.uuid4().hex
        expires_at = now + lifetime_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, expires_at FROM access_pairings WHERE code_hash = ?",
                (_digest(normalized),),
            ).fetchone()
            if row is None or row["expires_at"] <= now:
                if row is not None:
                    connection.execute("DELETE FROM access_pairings WHERE id = ?", (row["id"],))
                raise InvalidPairingCodeError("Pairing code is invalid or expired.")
            connection.execute("DELETE FROM access_pairings WHERE id = ?", (row["id"],))
            connection.execute(
                """
                INSERT INTO access_devices (
                    id, name, token_hash, scopes_json,
                    created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    device_name,
                    _digest(token),
                    json.dumps(["admin", "control", "read"]),
                    now,
                    now,
                    expires_at,
                ),
            )
        return token, {
            "id": device_id,
            "name": device_name,
            "scopes": ["admin", "control", "read"],
            "created_at": now,
            "last_seen_at": now,
            "expires_at": expires_at,
        }

    def create_device(
        self,
        *,
        device_name: str,
        scopes: set[str],
        lifetime_seconds: int,
    ) -> tuple[str, dict[str, object]]:
        """Create a persistent device session after a successful token login."""
        now = time.time()
        token = secrets.token_urlsafe(32)
        device = {
            "id": uuid.uuid4().hex,
            "name": device_name,
            "scopes": sorted(scopes & _KNOWN_SCOPES),
            "created_at": now,
            "last_seen_at": now,
            "expires_at": now + lifetime_seconds,
        }
        with self._connect() as connection:
            connection.execute("DELETE FROM access_devices WHERE expires_at <= ?", (now,))
            connection.execute(
                """
                INSERT INTO access_devices (
                    id, name, token_hash, scopes_json,
                    created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device["id"],
                    device["name"],
                    _digest(token),
                    json.dumps(device["scopes"]),
                    device["created_at"],
                    device["last_seen_at"],
                    device["expires_at"],
                ),
            )
        return token, device

    def authenticate_device(self, token: str | None) -> dict[str, object] | None:
        """Resolve a device token and opportunistically refresh its last-seen time."""
        if not token:
            return None
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM access_devices WHERE token_hash = ?",
                (_digest(token),),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                connection.execute("DELETE FROM access_devices WHERE id = ?", (row["id"],))
                return None
            if now - row["last_seen_at"] >= 3600:
                connection.execute(
                    "UPDATE access_devices SET last_seen_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM access_devices WHERE id = ?", (row["id"],)
                ).fetchone()
        return self._device_from_row(row)

    def list_devices(self) -> list[dict[str, object]]:
        """List active paired devices, newest first."""
        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM access_devices WHERE expires_at <= ?", (now,))
            rows = connection.execute(
                "SELECT * FROM access_devices ORDER BY created_at DESC"
            ).fetchall()
        return [self._device_from_row(row) for row in rows]

    def revoke_device(self, device_id: str) -> bool:
        """Revoke one persistent browser device."""
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM access_devices WHERE id = ?", (device_id,))
        return cursor.rowcount > 0
