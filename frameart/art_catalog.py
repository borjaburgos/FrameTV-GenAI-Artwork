"""Portable artwork metadata and an offline-first local ResourceSpace cache."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from frameart.resourcespace import PILOT_LIMIT, ResourceSpaceClient, ResourceSpaceError

_locks_guard = threading.Lock()
_sync_locks: dict[str, threading.Lock] = {}
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$"


def _optional_public_link(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Metadata references must be HTTP(S) links without credentials.")
    return value


class ArtistMetadata(BaseModel):
    """A credited artist, not a guessed identity from image recognition."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    id: str = Field(max_length=200)
    name: str = Field(min_length=1, max_length=300)
    bio: str | None = Field(None, max_length=5000)
    birth_year: int | None = Field(None, ge=-10000, le=3000)
    death_year: int | None = Field(None, ge=-10000, le=3000)
    nationality: str | None = Field(None, max_length=300)
    source_url: str | None = Field(None, max_length=2000)

    _source_link = field_validator("source_url")(_optional_public_link)


class ArtworkMetadata(BaseModel):
    """An artwork record independent of FrameArt generation-job IDs."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    schema_version: int = Field(1, ge=1, le=1)
    id: str = Field(pattern=_ID_PATTERN)
    title: str = Field(min_length=1, max_length=500)
    artist: ArtistMetadata
    date: str | None = Field(None, max_length=200)
    medium: str | None = Field(None, max_length=1000)
    movement: str | None = Field(None, max_length=500)
    institution: str | None = Field(None, max_length=300)
    accession_number: str | None = Field(None, max_length=200)
    work_dimensions: str | None = Field(None, max_length=2000)
    crop_notes: str | None = Field(None, max_length=3000)
    description: str | None = Field(None, max_length=10000)
    source_url: str | None = Field(None, max_length=2000)
    license: str | None = Field(None, max_length=1000)
    license_url: str | None = Field(None, max_length=2000)
    ai_generated: bool = False
    ai_provenance: str | None = Field(None, max_length=10000)
    source_publication_date: str | None = Field(None, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=100)
    width: int | None = Field(None, ge=1, le=100000)
    height: int | None = Field(None, ge=1, le=100000)
    original_ref: int | None = Field(None, ge=1)
    rendition_ref: int | None = Field(None, ge=1)

    _source_links = field_validator("source_url", "license_url")(_optional_public_link)

    @field_validator("tags")
    @classmethod
    def bounded_tags(cls, value: list[str]) -> list[str]:
        if any(len(tag) > 200 for tag in value):
            raise ValueError("Artwork tags must be at most 200 characters.")
        return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))


def artwork_from_fields(ref: int, fields: dict[str, str]) -> ArtworkMetadata:
    """Prefer the complete, versioned record; never fabricate missing history."""
    raw = fields.get("frameart_metadata")
    if raw:
        try:
            values = json.loads(raw)
            if not isinstance(values, dict):
                raise ValueError
            values["artist"] = dict(values.get("artist") or {})
            for field, key in (
                ("title", "title"),
                ("caption", "description"),
                ("frameart_date", "date"),
                ("frameart_medium", "medium"),
                ("frameart_license", "license"),
                ("frameart_source_url", "source_url"),
            ):
                if fields.get(field, "").strip():
                    values[key] = fields[field]
            for field, key in (("frameart_artist", "name"), ("frameart_artist_bio", "bio")):
                if fields.get(field, "").strip():
                    values["artist"][key] = fields[field]
            artwork = ArtworkMetadata.model_validate(values)
        except (ValidationError, ValueError, TypeError):
            raise ResourceSpaceError(
                "ResourceSpace contains invalid FrameArt artwork metadata."
            ) from None
    else:
        name = fields.get("frameart_artist") or fields.get("artist") or "Unknown artist"
        artwork = ArtworkMetadata(
            id=f"resourcespace-{ref}",
            title=fields.get("title") or f"Artwork {ref}",
            artist=ArtistMetadata(
                id="artist-" + hashlib.sha256(name.encode()).hexdigest()[:20],
                name=name,
            ),
            date=fields.get("frameart_date") or None,
            medium=fields.get("frameart_medium") or None,
            description=fields.get("caption") or fields.get("description") or None,
            license=fields.get("frameart_license") or fields.get("copyright") or None,
        )
    # The collection member is the requested rendition record. Originals stay
    # read-only on the NAS; the pilot importer retains their relative path and
    # checksum in ResourceSpace metadata, without uploading or indexing them.
    artwork.rendition_ref = ref
    return artwork


def _webp_thumbnail(payload: bytes) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError("Preview image is too large.")
                image = ImageOps.exif_transpose(image)
                image.thumbnail((800, 800))
                image = image.convert("RGB")
                destination = io.BytesIO()
                image.save(destination, format="WEBP", quality=85)
                return destination.getvalue()
    except Exception:
        raise ResourceSpaceError("ResourceSpace returned an invalid artwork preview.") from None


def integration_configured(settings) -> bool:
    key = settings.resourcespace_api_key
    return bool(
        settings.resourcespace_url
        and settings.resourcespace_user
        and key
        and key.get_secret_value()
        and settings.resourcespace_collection
    )


def _source_identity(settings) -> str:
    values = [
        settings.resourcespace_url,
        settings.resourcespace_user,
        settings.resourcespace_collection,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


class ArtCatalogStore:
    """Keep metadata and preview bytes local; a failed sync never empties it."""

    def __init__(self, data_dir: Path) -> None:
        self.database_path = Path(data_dir) / "frameart.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS art_catalog_items (
                    id TEXT PRIMARY KEY,
                    metadata TEXT NOT NULL,
                    thumbnail BLOB
                );
                CREATE TABLE IF NOT EXISTS art_catalog_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def snapshot(self, settings) -> dict[str, Any]:
        with self._connect() as connection:
            state = dict(connection.execute("SELECT key, value FROM art_catalog_state"))
            rows = connection.execute(
                "SELECT id, metadata, thumbnail IS NOT NULL AS has_thumbnail "
                "FROM art_catalog_items ORDER BY id LIMIT ?",
                (PILOT_LIMIT,),
            ).fetchall()
        items = []
        for row in rows:
            item = json.loads(row["metadata"])
            item["thumbnail_url"] = (
                f"/art-catalog/{row['id']}/thumbnail" if row["has_thumbnail"] else None
            )
            items.append(item)
        configured = integration_configured(settings)
        changed_source = bool(state.get("source") and state["source"] != _source_identity(settings))
        error = state.get("error")
        if not configured:
            status = "not_configured"
        elif error or changed_source:
            status = "stale"
        else:
            status = "ready" if items else "empty"
        return {
            "configured": configured,
            "status": status,
            "item_count": len(items),
            "items": items,
            "last_synced_at": state.get("last_synced_at"),
            "error": error,
            "pilot_limit": PILOT_LIMIT,
        }

    def replace(self, records: list[tuple[ArtworkMetadata, bytes | None]], settings) -> None:
        if not records or len(records) > PILOT_LIMIT:
            raise ResourceSpaceError("The pilot collection is empty or exceeds its size limit.")
        if len({record.id for record, _thumbnail in records}) != len(records):
            raise ResourceSpaceError("The pilot collection contains duplicate artwork IDs.")
        with self._connect() as connection:
            connection.execute("DELETE FROM art_catalog_items")
            connection.executemany(
                "INSERT INTO art_catalog_items (id, metadata, thumbnail) VALUES (?, ?, ?)",
                [(record.id, record.model_dump_json(), thumbnail) for record, thumbnail in records],
            )
            for key, value in (
                ("last_synced_at", datetime.now(timezone.utc).isoformat()),
                ("source", _source_identity(settings)),
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO art_catalog_state (key, value) VALUES (?, ?)",
                    (key, value),
                )
            connection.execute("DELETE FROM art_catalog_state WHERE key = 'error'")

    def record_error(self, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO art_catalog_state (key, value) VALUES ('error', ?)",
                (message,),
            )

    def thumbnail(self, artwork_id: str) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT thumbnail FROM art_catalog_items WHERE id = ?",
                (artwork_id,),
            ).fetchone()
        return row["thumbnail"] if row else None


class CatalogSyncBusyError(RuntimeError):
    """Another request is already refreshing this local catalog."""


def sync_catalog(settings) -> dict[str, Any]:
    """Manually refresh at most 20 records, without ever touching a TV or NAS."""
    if not integration_configured(settings):
        raise ResourceSpaceError(
            "Configure the ResourceSpace server, reader key and collection first."
        )
    lock_key = str(Path(settings.data_dir).resolve())
    with _locks_guard:
        lock = _sync_locks.setdefault(lock_key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise CatalogSyncBusyError("An artwork catalog refresh is already running.")
    try:
        store = ArtCatalogStore(settings.data_dir)
        try:
            client = ResourceSpaceClient(
                settings.resourcespace_url,
                settings.resourcespace_user,
                settings.resourcespace_api_key.get_secret_value(),
                timeout=settings.resourcespace_timeout,
            )
            resources = client.collection_resources(settings.resourcespace_collection)
            if not resources:
                raise ResourceSpaceError(
                    "ResourceSpace returned no artworks; the previous catalog was retained."
                )
            records = []
            for row in resources:
                try:
                    ref = int(row["ref"])
                    if ref < 1:
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    raise ResourceSpaceError(
                        "ResourceSpace returned an invalid artwork reference."
                    ) from None
                record = artwork_from_fields(ref, client.resource_fields(ref))
                preview = client.thumbnail(ref, str(row.get("preview_extension") or "jpg"))
                records.append((record, _webp_thumbnail(preview) if preview else None))
            store.replace(records, settings)
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, ResourceSpaceError)
                else (
                    "The artwork catalog could not be refreshed; the previous catalog was retained."
                )
            )
            store.record_error(message)
            raise ResourceSpaceError(message) from None
        return store.snapshot(settings)
    finally:
        lock.release()
