"""Durable TV groups, playlists, schedules, and integration events."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx2 as httpx

from frameart.library import LibraryStore
from frameart.logging_utils import safe_exception_message

logger = logging.getLogger(__name__)


class AutomationStore:
    """Persist automation definitions in FrameArt's shared SQLite database."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.database_path = self.data_dir / "frameart.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tv_groups (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    tv_profile_ids TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_playlists (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    job_ids TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    playlist_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    matte TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    next_run REAL NOT NULL,
                    current_index INTEGER NOT NULL DEFAULT 0,
                    last_run REAL,
                    last_status TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (playlist_id) REFERENCES automation_playlists(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (group_id) REFERENCES tv_groups(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_automation_schedules_due
                    ON automation_schedules(enabled, next_run);
                CREATE TABLE IF NOT EXISTS automation_webhooks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    url TEXT NOT NULL,
                    events TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
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
    def _decode(row: sqlite3.Row, *json_fields: str) -> dict[str, Any]:
        value = dict(row)
        for field in json_fields:
            value[field] = json.loads(value[field])
        for field in ("enabled",):
            if field in value:
                value[field] = bool(value[field])
        return value

    def list_groups(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tv_groups ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._decode(row, "tv_profile_ids") for row in rows]

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tv_groups WHERE id = ?", (group_id,)).fetchone()
        return self._decode(row, "tv_profile_ids") if row else None

    def create_group(self, name: str, tv_profile_ids: list[str]) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex,
            "name": name.strip(),
            "tv_profile_ids": list(dict.fromkeys(tv_profile_ids)),
            "created_at": time.time(),
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tv_groups (id, name, tv_profile_ids, created_at) VALUES (?, ?, ?, ?)",
                (item["id"], item["name"], json.dumps(item["tv_profile_ids"]), item["created_at"]),
            )
        return item

    def replace_tv_profile_ids(self, replacements: dict[str, str]) -> int:
        """Rewrite renamed or consolidated TV profile IDs in every group."""
        normalized = {
            source: target
            for source, target in replacements.items()
            if source and target and source != target
        }
        if not normalized:
            return 0

        updated = 0
        with self._connect() as connection:
            rows = connection.execute("SELECT id, tv_profile_ids FROM tv_groups").fetchall()
            for row in rows:
                current = json.loads(row["tv_profile_ids"])
                replacement = list(
                    dict.fromkeys(normalized.get(profile_id, profile_id) for profile_id in current)
                )
                if replacement == current:
                    continue
                connection.execute(
                    "UPDATE tv_groups SET tv_profile_ids = ? WHERE id = ?",
                    (json.dumps(replacement), row["id"]),
                )
                updated += 1
        return updated

    def delete_group(self, group_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM tv_groups WHERE id = ?", (group_id,))
        return cursor.rowcount > 0

    def list_playlists(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM automation_playlists ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._decode(row, "job_ids") for row in rows]

    def get_playlist(self, playlist_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_playlists WHERE id = ?", (playlist_id,)
            ).fetchone()
        return self._decode(row, "job_ids") if row else None

    def create_playlist(self, name: str, job_ids: list[str]) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex,
            "name": name.strip(),
            "job_ids": list(dict.fromkeys(job_ids)),
            "created_at": time.time(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_playlists (id, name, job_ids, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (item["id"], item["name"], json.dumps(item["job_ids"]), item["created_at"]),
            )
        return item

    def delete_playlist(self, playlist_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM automation_playlists WHERE id = ?", (playlist_id,)
            )
        return cursor.rowcount > 0

    def list_schedules(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM automation_schedules ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return self._decode(row) if row else None

    def create_schedule(
        self,
        *,
        name: str,
        playlist_id: str,
        group_id: str,
        interval_seconds: int,
        matte: str,
        enabled: bool,
    ) -> dict[str, Any]:
        now = time.time()
        item = {
            "id": uuid.uuid4().hex,
            "name": name.strip(),
            "playlist_id": playlist_id,
            "group_id": group_id,
            "interval_seconds": interval_seconds,
            "matte": matte,
            "enabled": enabled,
            "next_run": now + interval_seconds,
            "current_index": 0,
            "last_run": None,
            "last_status": None,
            "last_error": None,
            "created_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_schedules (
                    id, name, playlist_id, group_id, interval_seconds, matte, enabled,
                    next_run, current_index, last_run, last_status, last_error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?)
                """,
                (
                    item["id"], item["name"], playlist_id, group_id, interval_seconds,
                    matte, int(enabled), item["next_run"], now,
                ),
            )
        return item

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE automation_schedules SET enabled = ?, next_run = ?
                WHERE id = ?
                """,
                (int(enabled), time.time() + 1, schedule_id),
            )
        return cursor.rowcount > 0

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM automation_schedules WHERE id = ?", (schedule_id,)
            )
        return cursor.rowcount > 0

    def due_schedule_ids(self, now: float | None = None) -> list[str]:
        cutoff = time.time() if now is None else now
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM automation_schedules
                WHERE enabled = 1 AND next_run <= ? ORDER BY next_run LIMIT 20
                """,
                (cutoff,),
            ).fetchall()
        return [row["id"] for row in rows]

    def finish_schedule(
        self,
        schedule_id: str,
        *,
        current_index: int,
        status: str,
        error: str | None,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                "SELECT interval_seconds FROM automation_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
            if not row:
                return
            connection.execute(
                """
                UPDATE automation_schedules
                SET current_index = ?, last_run = ?, last_status = ?, last_error = ?, next_run = ?
                WHERE id = ?
                """,
                (
                    current_index, timestamp, status, error,
                    timestamp + int(row["interval_seconds"]), schedule_id,
                ),
            )

    def list_webhooks(self, *, include_secrets: bool = False) -> list[dict[str, Any]]:
        fields = "*" if include_secrets else "id, name, url, events, enabled, created_at"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {fields} FROM automation_webhooks ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._decode(row, "events") for row in rows]

    def create_webhook(self, name: str, url: str, events: list[str]) -> dict[str, Any]:
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        item = {
            "id": uuid.uuid4().hex,
            "name": name.strip(),
            "url": url,
            "events": list(dict.fromkeys(events)),
            "secret": secret,
            "enabled": True,
            "created_at": time.time(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_webhooks
                    (id, name, url, events, secret, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    item["id"], item["name"], item["url"], json.dumps(item["events"]),
                    secret, item["created_at"],
                ),
            )
        return item

    def delete_webhook(self, webhook_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM automation_webhooks WHERE id = ?", (webhook_id,)
            )
        return cursor.rowcount > 0


def _find_artifact_image(data_dir: Path, job_id: str) -> Path:
    artifacts = Path(data_dir) / "artifacts"
    for candidate in artifacts.rglob(job_id):
        if not candidate.is_dir() or candidate.name != job_id:
            continue
        final_path = candidate / "final.png"
        source_path = candidate / "source.png"
        if final_path.is_file():
            return final_path
        if source_path.is_file():
            return source_path
    raise FileNotFoundError(f"Artwork job {job_id!r} does not have an image.")


def display_artifact(settings, job_id: str, tv_profile_id: str, matte: str) -> dict[str, Any]:
    """Display a library artifact on one TV, reusing an existing TV upload when possible."""
    from frameart.tv.controller import list_art_deduplicated, switch_art, upload_image

    profile = settings.tvs.get(tv_profile_id)
    if profile is None:
        raise KeyError(f"TV profile {tv_profile_id!r} is no longer configured.")
    image_path = _find_artifact_image(settings.data_dir, job_id)
    meta_path = image_path.parent / "meta.json"
    try:
        metadata = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    except (OSError, ValueError):
        metadata = {}

    tv_map = metadata.get("tv_content_ids")
    if not isinstance(tv_map, dict):
        tv_map = {}
    content_id = tv_map.get(profile.ip)
    reused = False
    if isinstance(content_id, str) and content_id:
        try:
            available = {str(item.get("content_id", "")) for item in list_art_deduplicated(profile)}
            reused = content_id in available and switch_art(profile, content_id)
        except Exception:
            reused = False

    if not reused:
        file_type = "JPEG" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "PNG"
        upload = upload_image(profile, image_path.read_bytes(), file_type=file_type, matte=matte)
        if not upload.success or not upload.content_id:
            raise RuntimeError(upload.error or "TV upload failed.")
        content_id = upload.content_id
        if not switch_art(profile, content_id):
            raise RuntimeError("TV accepted the upload but did not switch artwork.")
        tv_map[profile.ip] = content_id
        metadata["tv_content_ids"] = tv_map
        meta_path.write_text(json.dumps(metadata, indent=2, default=str))

    LibraryStore(settings.data_dir).record_display(
        job_id=job_id,
        content_id=content_id,
        tv_target=tv_profile_id,
        source="automation-playlist",
    )
    return {
        "tv_profile_id": tv_profile_id,
        "content_id": content_id,
        "reused_existing_content": reused,
    }


class IntegrationPublisher:
    """Deliver signed webhook events and optional MQTT state messages."""

    def __init__(self, store: AutomationStore) -> None:
        self.store = store

    def publish(self, event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        envelope = {
            "id": uuid.uuid4().hex,
            "event": event,
            "created_at": time.time(),
            "data": payload,
        }
        body = json.dumps(envelope, separators=(",", ":"), default=str).encode()
        deliveries: list[dict[str, Any]] = []
        for webhook in self.store.list_webhooks(include_secrets=True):
            if not webhook["enabled"] or event not in webhook["events"]:
                continue
            signature = hmac.new(webhook["secret"].encode(), body, hashlib.sha256).hexdigest()
            try:
                response = httpx.post(
                    webhook["url"],
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-FrameArt-Event": event,
                        "X-FrameArt-Signature": f"sha256={signature}",
                    },
                    timeout=5,
                )
                response.raise_for_status()
                deliveries.append({"webhook_id": webhook["id"], "ok": True})
            except Exception as exc:
                safe_error = safe_exception_message(
                    exc,
                    secrets=[webhook.get("secret", "")],
                )
                logger.warning("Webhook %s delivery failed: %s", webhook["name"], safe_error)
                deliveries.append(
                    {"webhook_id": webhook["id"], "ok": False, "error": safe_error}
                )
        self._publish_mqtt(envelope)
        return deliveries

    @staticmethod
    def mqtt_status() -> dict[str, Any]:
        broker = os.environ.get("FRAMEART_MQTT_BROKER")
        try:
            import paho.mqtt.publish  # noqa: F401

            installed = True
        except ImportError:
            installed = False
        return {
            "configured": bool(broker),
            "dependency_installed": installed,
            "broker": broker,
            "topic_prefix": os.environ.get("FRAMEART_MQTT_TOPIC_PREFIX", "frameart"),
        }

    @staticmethod
    def _publish_mqtt(envelope: dict[str, Any]) -> None:
        broker = os.environ.get("FRAMEART_MQTT_BROKER")
        if not broker:
            return
        try:
            import paho.mqtt.publish as publish

            prefix = os.environ.get("FRAMEART_MQTT_TOPIC_PREFIX", "frameart").strip("/")
            auth = None
            username = os.environ.get("FRAMEART_MQTT_USERNAME")
            if username:
                auth = {
                    "username": username,
                    "password": os.environ.get("FRAMEART_MQTT_PASSWORD"),
                }
            publish.single(
                f"{prefix}/events/{envelope['event']}",
                json.dumps(envelope, separators=(",", ":"), default=str),
                hostname=broker,
                port=int(os.environ.get("FRAMEART_MQTT_PORT", "1883")),
                qos=1,
                retain=False,
                auth=auth,
            )
        except Exception as exc:
            logger.warning("MQTT publish failed: %s", safe_exception_message(exc))


class AutomationScheduler:
    """Run due playlists from a single daemon thread."""

    def __init__(self, settings_loader, *, poll_seconds: float = 5.0) -> None:
        self.settings_loader = settings_loader
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="frameart-automation",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(self.poll_seconds + 1, 2))

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                logger.error(
                    "Automation scheduler tick failed: %s",
                    safe_exception_message(exc),
                )

    def tick(self, now: float | None = None) -> list[dict[str, Any]]:
        settings = self.settings_loader()
        store = AutomationStore(settings.data_dir)
        results: list[dict[str, Any]] = []
        for schedule_id in store.due_schedule_ids(now):
            try:
                results.append(self.run_schedule(schedule_id))
            except Exception as exc:
                logger.error(
                    "Scheduled run failed unexpectedly for %s: %s",
                    schedule_id,
                    safe_exception_message(exc),
                )
        return results

    def run_schedule(self, schedule_id: str) -> dict[str, Any]:
        with self._run_lock:
            settings = self.settings_loader()
            store = AutomationStore(settings.data_dir)
            schedule = store.get_schedule(schedule_id)
            if schedule is None:
                raise KeyError(schedule_id)
            playlist = store.get_playlist(schedule["playlist_id"])
            group = store.get_group(schedule["group_id"])
            if playlist is None or group is None:
                raise RuntimeError("The schedule's playlist or TV group no longer exists.")
            job_ids = playlist["job_ids"]
            if not job_ids:
                error = "The scheduled playlist is empty."
                store.finish_schedule(
                    schedule_id,
                    current_index=0,
                    status="failed",
                    error=error,
                )
                payload = {
                    "schedule_id": schedule_id,
                    "schedule_name": schedule["name"],
                    "playlist_id": playlist["id"],
                    "group_id": group["id"],
                    "job_id": None,
                    "status": "failed",
                    "results": [],
                    "errors": [error],
                }
                IntegrationPublisher(store).publish("schedule.failed", payload)
                return payload
            index = int(schedule["current_index"]) % len(job_ids)
            job_id = job_ids[index]
            results: list[dict[str, Any]] = []
            errors: list[str] = []
            for tv_profile_id in group["tv_profile_ids"]:
                try:
                    results.append(
                        display_artifact(settings, job_id, tv_profile_id, schedule["matte"])
                    )
                except Exception as exc:
                    errors.append(f"{tv_profile_id}: {exc}")
            status = "completed" if results and not errors else ("partial" if results else "failed")
            error = "; ".join(errors) or None
            store.finish_schedule(
                schedule_id,
                current_index=(index + 1) % len(job_ids),
                status=status,
                error=error,
            )
            payload = {
                "schedule_id": schedule_id,
                "schedule_name": schedule["name"],
                "playlist_id": playlist["id"],
                "group_id": group["id"],
                "job_id": job_id,
                "status": status,
                "results": results,
                "errors": errors,
            }
            IntegrationPublisher(store).publish(f"schedule.{status}", payload)
            return payload
