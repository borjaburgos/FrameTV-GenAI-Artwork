"""Synchronize public Apple and Google photo albums to Frame TVs.

The provider adapters intentionally return a complete snapshot of the newest
items.  A failed fetch is never represented as an empty album: that distinction
prevents transient provider failures from deleting TV artwork.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx2 as httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from frameart.automation import AutomationStore, IntegrationPublisher

logger = logging.getLogger(__name__)

MAX_SYNC_PHOTOS = 10
_MAX_PAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_BYTES = 30 * 1024 * 1024
_MAX_IMAGE_PIXELS = 50_000_000
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ICLOUD_PAGE_HOSTS = {"icloud.com", "www.icloud.com"}
_GOOGLE_PAGE_HOSTS = {"photos.app.goo.gl", "photos.google.com"}
_ICLOUD_STREAM_HOST_RE = re.compile(r"^p\d+-sharedstreams\.icloud\.com$")
_ICLOUD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,512}$")
_ALBUM_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_CACHE_NAME_RE = re.compile(r"^[a-f0-9]{24}-[a-f0-9]{16}\.jpg$")


@dataclass(frozen=True)
class AlbumItem:
    """One provider-neutral still image in a public album snapshot."""

    item_id: str
    image_url: str
    added_at: float
    checksum: str
    title: str = ""


@dataclass(frozen=True)
class AlbumSnapshot:
    """A successfully loaded, deletion-aware public album snapshot."""

    name: str
    total_count: int
    items: tuple[AlbumItem, ...]


def _album_cache_directory(data_dir: Path, album_id: str) -> Path:
    if not _ALBUM_ID_RE.fullmatch(album_id):
        raise ValueError("Live album ID is invalid.")
    return Path(data_dir) / "cache" / "live-albums" / album_id


def _album_cache_name(item: AlbumItem) -> str:
    item_digest = hashlib.sha256(item.item_id.encode()).hexdigest()[:24]
    version_digest = hashlib.sha256(item.checksum.encode()).hexdigest()[:16]
    return f"{item_digest}-{version_digest}.jpg"


def _clean_https_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise ValueError("Album URL is invalid.") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Album URL must be an absolute HTTPS URL.")
    try:
        unsafe_authority = parsed.username or parsed.password or parsed.port not in {None, 443}
    except ValueError as exc:
        raise ValueError("Album URL has an invalid port.") from exc
    if unsafe_authority:
        raise ValueError("Album URLs cannot contain credentials or a custom port.")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, parsed.fragment))


def _icloud_token(source_url: str) -> str:
    clean = _clean_https_url(source_url)
    parsed = urlsplit(clean)
    if parsed.hostname not in _ICLOUD_PAGE_HOSTS or not parsed.path.startswith(
        ("/sharedalbum", "/photostream")
    ):
        raise ValueError("Use an iCloud public Shared Album URL.")
    token = parsed.fragment.split(";", 1)[0]
    if not _ICLOUD_TOKEN_RE.fullmatch(token):
        raise ValueError("The iCloud Shared Album URL is missing its public token.")
    return token


def validate_album_url(provider: str, value: str) -> str:
    """Validate a provider URL without fetching it."""
    clean = _clean_https_url(value)
    host = urlsplit(clean).hostname
    if provider == "icloud":
        _icloud_token(clean)
    elif provider == "google_photos":
        if host not in _GOOGLE_PAGE_HOSTS:
            raise ValueError(
                "Use a public Google Photos URL on photos.app.goo.gl or photos.google.com."
            )
    else:
        raise ValueError(f"Unsupported photo album provider: {provider}")
    return clean


def _response_bytes(response, *, limit: int, label: str) -> bytes:
    try:
        declared = int(response.headers.get("content-length", "0"))
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise ValueError(f"{label} exceeds the {limit // (1024 * 1024)} MB limit.")
    body = response.content
    if len(body) > limit:
        raise ValueError(f"{label} exceeds the {limit // (1024 * 1024)} MB limit.")
    return body


def _json_response(response, *, label: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"{label} returned HTTP {response.status_code}.") from exc
    body = _response_bytes(response, limit=_MAX_PAGE_BYTES, label=label)
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} returned an invalid response.") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned an invalid response.")
    return value


def _decode_json_response(response, *, label: str) -> dict[str, Any]:
    """Decode JSON for Apple's non-standard HTTP 330 redirect response."""
    body = _response_bytes(response, limit=_MAX_PAGE_BYTES, label=label)
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} returned an invalid response.") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned an invalid response.")
    return value


def _base62_to_int(value: str) -> int:
    total = 0
    for character in value:
        index = _BASE62.find(character)
        if index < 0:
            raise ValueError("The iCloud Shared Album token is invalid.")
        total = total * 62 + index
    return total


def _icloud_partition(token: str) -> int:
    partition_text = token[1] if token.startswith("A") else token[1:3]
    return _base62_to_int(partition_text)


def _icloud_stream_base(token: str) -> str:
    return f"https://p{_icloud_partition(token):02d}-sharedstreams.icloud.com/{token}/sharedstreams"


def _valid_icloud_stream_host(host: str | None) -> bool:
    return bool(host and _ICLOUD_STREAM_HOST_RE.fullmatch(host))


def _post_icloud(base_url: str, endpoint: str, payload: dict[str, Any]) -> tuple[dict, str]:
    url = f"{base_url}/{endpoint}"
    response = httpx.post(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Content-Type": "text/plain",
            "User-Agent": "FrameArt/0.2",
        },
        content=json.dumps(payload).encode(),
        follow_redirects=False,
        timeout=25,
    )
    if response.status_code == 330:
        redirect_payload = _decode_json_response(response, label="iCloud Shared Album")
        redirect_host = redirect_payload.get("X-Apple-MMe-Host")
        if not _valid_icloud_stream_host(redirect_host):
            raise RuntimeError("iCloud returned an unsafe Shared Album redirect.")
        token = urlsplit(base_url).path.split("/")[1]
        base_url = f"https://{redirect_host}/{token}/sharedstreams"
        response = httpx.post(
            f"{base_url}/{endpoint}",
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Content-Type": "text/plain",
                "User-Agent": "FrameArt/0.2",
            },
            content=json.dumps(payload).encode(),
            follow_redirects=False,
            timeout=25,
        )
    return _json_response(response, label="iCloud Shared Album"), base_url


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _best_icloud_derivative(row: dict[str, Any]) -> tuple[str, str] | None:
    derivatives = row.get("derivatives")
    if not isinstance(derivatives, dict):
        return None
    candidates: list[tuple[int, int, str, str]] = []
    for name, value in derivatives.items():
        if not isinstance(value, dict) or not value.get("checksum"):
            continue
        try:
            width = int(value.get("width") or 0)
            height = int(value.get("height") or 0)
        except (TypeError, ValueError):
            continue
        candidates.append((width * height, max(width, height), str(name), str(value["checksum"])))
    if not candidates:
        return None
    _pixels, _edge, name, checksum = max(candidates)
    return name, checksum


def _icloud_asset_url(payload: dict[str, Any], checksum: str) -> str:
    entries = payload.get("items")
    locations = payload.get("locations")
    if not isinstance(entries, dict) or not isinstance(locations, dict):
        raise RuntimeError("iCloud did not return photo download locations.")
    value = entries.get(checksum)
    if not isinstance(value, dict):
        raise RuntimeError("iCloud did not return a requested photo derivative.")
    location_name = value.get("url_location")
    location = locations.get(location_name)
    if not isinstance(location, dict):
        raise RuntimeError("iCloud returned an invalid photo download location.")
    hosts = location.get("hosts")
    host = hosts[0] if isinstance(hosts, list) and hosts else location_name
    if not isinstance(host, str) or not (
        host == "icloud-content.com" or host.endswith(".icloud-content.com")
    ):
        raise RuntimeError("iCloud returned an unsafe photo download location.")
    path = value.get("url_path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise RuntimeError("iCloud returned an invalid photo download path.")
    return f"https://{host}{path}"


def fetch_icloud_album(source_url: str, *, limit: int = MAX_SYNC_PHOTOS) -> AlbumSnapshot:
    """Fetch the newest stills from an iCloud public Shared Album."""
    token = _icloud_token(source_url)
    stream, base_url = _post_icloud(
        _icloud_stream_base(token), "webstream", {"streamCtag": None}
    )
    rows = stream.get("photos")
    if not isinstance(rows, list):
        raise RuntimeError("iCloud Shared Album did not return a photo list.")
    photos: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("mediaAssetType", "")).lower() == "video":
            continue
        guid = row.get("photoGuid")
        derivative = _best_icloud_derivative(row)
        if not isinstance(guid, str) or not guid or derivative is None:
            continue
        photos.append(
            {
                "item_id": guid,
                "added_at": _timestamp(row.get("batchDateCreated") or row.get("dateCreated")),
                "title": str(row.get("caption") or "Shared album photo")[:300],
                "checksum": derivative[1],
            }
        )
    photos.sort(key=lambda item: (item["added_at"], item["item_id"]), reverse=True)
    desired = photos[:limit]
    if not desired:
        return AlbumSnapshot(str(stream.get("streamName") or "iCloud Shared Album"), 0, ())
    assets, _ = _post_icloud(
        base_url,
        "webasseturls",
        {"photoGuids": [item["item_id"] for item in desired]},
    )
    items = tuple(
        AlbumItem(
            item_id=item["item_id"],
            image_url=_icloud_asset_url(assets, item["checksum"]),
            added_at=item["added_at"],
            checksum=item["checksum"],
            title=item["title"],
        )
        for item in desired
    )
    return AlbumSnapshot(str(stream.get("streamName") or "iCloud Shared Album"), len(photos), items)


def _get_google_page(source_url: str) -> str:
    current = validate_album_url("google_photos", source_url)
    for _ in range(6):
        response = httpx.get(
            current,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Mozilla/5.0 (compatible; FrameArt/0.2)",
            },
            follow_redirects=False,
            timeout=25,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            try:
                response.raise_for_status()
            except Exception as exc:
                raise RuntimeError(
                    f"Google Photos returned HTTP {response.status_code}."
                ) from exc
            return _response_bytes(
                response, limit=_MAX_PAGE_BYTES, label="Google Photos album page"
            ).decode("utf-8", errors="replace")
        location = response.headers.get("location")
        if not location:
            raise RuntimeError("Google Photos returned an invalid redirect.")
        redirected = _clean_https_url(urljoin(current, location))
        if urlsplit(redirected).hostname not in _GOOGLE_PAGE_HOSTS:
            raise RuntimeError("Google Photos returned an unsafe redirect.")
        current = redirected
    raise RuntimeError("Google Photos exceeded the redirect limit.")


def _balanced_json_array(source: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(source)):
        character = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    return None


def _google_data_arrays(page: str) -> list[list[Any]]:
    arrays: list[list[Any]] = []
    for callback in re.finditer(r"AF_initDataCallback\s*\(", page):
        end = page.find("</script>", callback.end())
        if end < 0:
            continue
        block = page[callback.end() : end]
        match = re.search(r"\bdata\s*:\s*(\[)", block)
        if not match:
            continue
        encoded = _balanced_json_array(block, match.start(1))
        if not encoded:
            continue
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            arrays.append(value)
    return arrays


def _google_asset_url(value: str, width: int, height: int) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (
        host == "googleusercontent.com" or host.endswith(".googleusercontent.com")
    ):
        raise ValueError("Google Photos returned an unsafe image location.")
    base = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    max_width = min(max(width, 1), 3840)
    max_height = min(max(height, 1), 2160)
    return f"{base}=w{max_width}-h{max_height}"


def parse_google_album_page(page: str, *, limit: int = MAX_SYNC_PHOTOS) -> AlbumSnapshot:
    """Parse still-photo metadata embedded in a public Google Photos page."""
    photos: dict[str, AlbumItem] = {}
    found_album_data = False
    for data in _google_data_arrays(page):
        if len(data) < 2 or not isinstance(data[1], list):
            continue
        rows = data[1]
        if not rows or any(isinstance(row, list) and len(row) >= 6 for row in rows):
            found_album_data = True
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            uid, detail = row[0], row[1]
            if not isinstance(uid, str) or not isinstance(detail, list) or len(detail) < 3:
                continue
            base_url, width, height = detail[:3]
            if not (
                isinstance(base_url, str)
                and isinstance(width, (int, float))
                and isinstance(height, (int, float))
            ):
                continue
            video_metadata = row[9] if len(row) > 9 else None
            if isinstance(video_metadata, dict) and "76647426" in video_metadata:
                continue
            added_ms = row[5] if isinstance(row[5], (int, float)) else 0
            updated_ms = row[2] if isinstance(row[2], (int, float)) else 0
            try:
                image_url = _google_asset_url(base_url, int(width), int(height))
            except ValueError:
                continue
            checksum = hashlib.sha256(f"{uid}:{updated_ms}".encode()).hexdigest()
            photos[uid] = AlbumItem(
                item_id=uid,
                image_url=image_url,
                added_at=float(added_ms) / 1000,
                checksum=checksum,
                title="Google Photos album photo",
            )
    if not found_album_data:
        raise RuntimeError(
            "Google Photos did not expose public album data. Confirm link sharing is enabled."
        )
    ordered = sorted(photos.values(), key=lambda item: (item.added_at, item.item_id), reverse=True)
    title_match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)',
        page,
        re.IGNORECASE,
    )
    name = title_match.group(1) if title_match else "Google Photos Shared Album"
    return AlbumSnapshot(name=name, total_count=len(ordered), items=tuple(ordered[:limit]))


def fetch_google_photos_album(
    source_url: str, *, limit: int = MAX_SYNC_PHOTOS
) -> AlbumSnapshot:
    return parse_google_album_page(_get_google_page(source_url), limit=limit)


def fetch_album_items(provider: str, source_url: str) -> AlbumSnapshot:
    """Fetch one public provider snapshot, newest first."""
    validate_album_url(provider, source_url)
    if provider == "icloud":
        return fetch_icloud_album(source_url)
    if provider == "google_photos":
        return fetch_google_photos_album(source_url)
    raise ValueError(f"Unsupported photo album provider: {provider}")


def _valid_asset_host(provider: str, host: str | None) -> bool:
    if not host:
        return False
    if provider == "icloud":
        return host == "icloud-content.com" or host.endswith(".icloud-content.com")
    if provider == "google_photos":
        return host == "googleusercontent.com" or host.endswith(".googleusercontent.com")
    return False


def download_album_image(provider: str, item: AlbumItem) -> bytes:
    """Download and normalize one provider image to a bounded TV-ready JPEG."""
    current = item.image_url
    for _ in range(4):
        parsed = urlsplit(current)
        if parsed.scheme != "https" or not _valid_asset_host(provider, parsed.hostname):
            raise RuntimeError(f"{provider} returned an unsafe image URL.")
        response = httpx.get(
            current,
            headers={"Accept": "image/*", "User-Agent": "FrameArt/0.2"},
            follow_redirects=False,
            timeout=30,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            try:
                response.raise_for_status()
            except Exception as exc:
                raise RuntimeError(
                    f"Album photo download returned HTTP {response.status_code}."
                ) from exc
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not content_type.startswith("image/"):
                raise ValueError("Public album item did not return an image.")
            body = _response_bytes(
                response, limit=_MAX_IMAGE_BYTES, label="Public album photo"
            )
            break
        location = response.headers.get("location")
        if not location:
            raise RuntimeError("Album photo server returned an invalid redirect.")
        current = urljoin(current, location)
    else:
        raise RuntimeError("Album photo download exceeded the redirect limit.")
    try:
        with Image.open(io.BytesIO(body)) as source:
            if source.width * source.height > _MAX_IMAGE_PIXELS:
                raise ValueError("Public album photo exceeds the 50-megapixel limit.")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((3840, 2160), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=94, optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Public album item is not a supported still image.") from exc
    return output.getvalue()


class LiveAlbumStore:
    """Persist live-album configuration and the TV content it owns."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.database_path = self.data_dir / "frameart.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS photo_album_feeds (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    provider TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    display_newest INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    next_sync REAL NOT NULL,
                    last_sync REAL,
                    last_status TEXT,
                    last_error TEXT,
                    source_name TEXT,
                    source_count INTEGER,
                    synced_count INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_photo_album_feeds_due
                    ON photo_album_feeds(enabled, next_sync);
                CREATE TABLE IF NOT EXISTS photo_album_source_items (
                    album_id TEXT NOT NULL,
                    source_item_id TEXT NOT NULL,
                    source_checksum TEXT NOT NULL,
                    added_at REAL NOT NULL,
                    title TEXT NOT NULL,
                    cache_name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (album_id, source_item_id),
                    FOREIGN KEY (album_id) REFERENCES photo_album_feeds(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS photo_album_tv_items (
                    album_id TEXT NOT NULL,
                    tv_profile_id TEXT NOT NULL,
                    source_item_id TEXT NOT NULL,
                    source_checksum TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    added_at REAL NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (album_id, tv_profile_id, source_item_id),
                    FOREIGN KEY (album_id) REFERENCES photo_album_feeds(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS photo_album_stale_items (
                    album_id TEXT NOT NULL,
                    tv_profile_id TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (album_id, tv_profile_id, content_id),
                    FOREIGN KEY (album_id) REFERENCES photo_album_feeds(id) ON DELETE CASCADE
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
    def _decode(row: sqlite3.Row, *, include_source: bool = False) -> dict[str, Any]:
        item = dict(row)
        source_url = item.pop("source_url")
        item["source_host"] = urlsplit(source_url).hostname
        item["has_source_url"] = bool(source_url)
        if include_source:
            item["source_url"] = source_url
        item["enabled"] = bool(item["enabled"])
        item["display_newest"] = bool(item["display_newest"])
        item["group_id"] = item["target_id"] if item["target_type"] == "group" else None
        item["tv_profile_id"] = item["target_id"] if item["target_type"] == "tv" else None
        return item

    def list_albums(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM photo_album_feeds ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def get_album(self, album_id: str, *, include_source: bool = False) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM photo_album_feeds WHERE id = ?", (album_id,)
            ).fetchone()
        return self._decode(row, include_source=include_source) if row else None

    def create_album(
        self,
        *,
        name: str,
        provider: str,
        source_url: str,
        target_type: str,
        target_id: str,
        interval_seconds: int,
        display_newest: bool,
        enabled: bool,
    ) -> dict[str, Any]:
        now = time.time()
        album_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO photo_album_feeds (
                    id, name, provider, source_url, target_type, target_id,
                    interval_seconds, display_newest, enabled, next_sync,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    album_id,
                    name.strip(),
                    provider,
                    source_url,
                    target_type,
                    target_id,
                    interval_seconds,
                    int(display_newest),
                    int(enabled),
                    now,
                    now,
                    now,
                ),
            )
        return self.get_album(album_id) or {"id": album_id}

    def update_album(
        self,
        album_id: str,
        *,
        name: str,
        provider: str,
        source_url: str,
        target_type: str,
        target_id: str,
        interval_seconds: int,
        display_newest: bool,
        enabled: bool,
    ) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE photo_album_feeds
                SET name = ?, provider = ?, source_url = ?, target_type = ?, target_id = ?,
                    interval_seconds = ?, display_newest = ?, enabled = ?, next_sync = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name.strip(),
                    provider,
                    source_url,
                    target_type,
                    target_id,
                    interval_seconds,
                    int(display_newest),
                    int(enabled),
                    now,
                    now,
                    album_id,
                ),
            )
        return cursor.rowcount > 0

    def set_enabled(self, album_id: str, enabled: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE photo_album_feeds
                   SET enabled = ?, next_sync = ?, updated_at = ? WHERE id = ?""",
                (int(enabled), time.time(), time.time(), album_id),
            )
        return cursor.rowcount > 0

    def due_album_ids(self, now: float | None = None) -> list[str]:
        cutoff = time.time() if now is None else now
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id FROM photo_album_feeds
                   WHERE enabled = 1 AND next_sync <= ? ORDER BY next_sync LIMIT 20""",
                (cutoff,),
            ).fetchall()
        return [row["id"] for row in rows]

    def update_runtime(
        self,
        album_id: str,
        *,
        next_sync: float,
        status: str,
        error: str | None,
        source_name: str | None = None,
        source_count: int | None = None,
        synced_count: int | None = None,
    ) -> None:
        assignments = [
            "next_sync = ?",
            "last_sync = ?",
            "last_status = ?",
            "last_error = ?",
            "updated_at = ?",
        ]
        now = time.time()
        values: list[Any] = [next_sync, now, status, error, now]
        for field, value in (
            ("source_name", source_name),
            ("source_count", source_count),
            ("synced_count", synced_count),
        ):
            if value is not None:
                assignments.append(f"{field} = ?")
                values.append(value)
        values.append(album_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE photo_album_feeds SET {', '.join(assignments)} WHERE id = ?", values
            )

    def source_items(self, album_id: str) -> list[dict[str, Any]]:
        """Return the bounded cached provider snapshot without exposing local paths."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM photo_album_source_items
                   WHERE album_id = ? ORDER BY added_at DESC, source_item_id DESC""",
                (album_id,),
            ).fetchall()
            uploads = connection.execute(
                """SELECT source_item_id, source_checksum, tv_profile_id
                   FROM photo_album_tv_items WHERE album_id = ?""",
                (album_id,),
            ).fetchall()
        uploaded_to: dict[str, list[str]] = {}
        checksums = {row["source_item_id"]: row["source_checksum"] for row in rows}
        for upload in uploads:
            item_id = upload["source_item_id"]
            if checksums.get(item_id) == upload["source_checksum"]:
                uploaded_to.setdefault(item_id, []).append(upload["tv_profile_id"])
        return [
            {
                "item_id": row["source_item_id"],
                "added_at": row["added_at"],
                "title": row["title"],
                "version": hashlib.sha256(
                    row["source_checksum"].encode()
                ).hexdigest()[:16],
                "uploaded_to": sorted(uploaded_to.get(row["source_item_id"], [])),
            }
            for row in rows
        ]

    def source_item(self, album_id: str, item_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM photo_album_source_items
                   WHERE album_id = ? AND source_item_id = ?""",
                (album_id, item_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        cache_name = item.pop("cache_name")
        if not _CACHE_NAME_RE.fullmatch(cache_name):
            raise RuntimeError("Live album preview metadata is invalid.")
        item["cache_path"] = _album_cache_directory(
            self.data_dir, album_id
        ) / cache_name
        return item

    def replace_source_items(self, album_id: str, items: tuple[AlbumItem, ...]) -> None:
        """Atomically replace the gallery snapshot after every image is cached."""
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM photo_album_source_items WHERE album_id = ?", (album_id,)
            )
            connection.executemany(
                """INSERT INTO photo_album_source_items (
                       album_id, source_item_id, source_checksum, added_at,
                       title, cache_name, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        album_id,
                        item.item_id,
                        item.checksum,
                        item.added_at,
                        item.title,
                        _album_cache_name(item),
                        now,
                        now,
                    )
                    for item in items
                ],
            )

    def clear_source_cache(self, album_id: str) -> None:
        cache_dir = _album_cache_directory(self.data_dir, album_id)
        if not cache_dir.is_dir():
            return
        for path in cache_dir.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)
        try:
            cache_dir.rmdir()
        except OSError:
            logger.warning("Could not remove live album cache directory %s", cache_dir)

    def current_items(self, album_id: str, tv_profile_id: str) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM photo_album_tv_items
                   WHERE album_id = ? AND tv_profile_id = ?""",
                (album_id, tv_profile_id),
            ).fetchall()
        return {row["source_item_id"]: dict(row) for row in rows}

    def stale_ids(self, album_id: str, tv_profile_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT content_id FROM photo_album_stale_items
                   WHERE album_id = ? AND tv_profile_id = ? ORDER BY created_at""",
                (album_id, tv_profile_id),
            ).fetchall()
        return [row["content_id"] for row in rows]

    def owned_profile_ids(self, album_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT tv_profile_id FROM photo_album_tv_items WHERE album_id = ?
                UNION
                SELECT tv_profile_id FROM photo_album_stale_items WHERE album_id = ?
                """,
                (album_id, album_id),
            ).fetchall()
        return [row["tv_profile_id"] for row in rows]

    def upsert_current(self, album_id: str, tv_profile_id: str, item: AlbumItem, content_id: str):
        with self._connect() as connection:
            old = connection.execute(
                """SELECT content_id FROM photo_album_tv_items
                   WHERE album_id = ? AND tv_profile_id = ? AND source_item_id = ?""",
                (album_id, tv_profile_id, item.item_id),
            ).fetchone()
            if old and old["content_id"] != content_id:
                connection.execute(
                    """INSERT OR IGNORE INTO photo_album_stale_items
                       (album_id, tv_profile_id, content_id, created_at) VALUES (?, ?, ?, ?)""",
                    (album_id, tv_profile_id, old["content_id"], time.time()),
                )
            connection.execute(
                """
                INSERT INTO photo_album_tv_items (
                    album_id, tv_profile_id, source_item_id, source_checksum,
                    content_id, added_at, title, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(album_id, tv_profile_id, source_item_id) DO UPDATE SET
                    source_checksum = excluded.source_checksum,
                    content_id = excluded.content_id,
                    added_at = excluded.added_at,
                    title = excluded.title,
                    created_at = excluded.created_at
                """,
                (
                    album_id,
                    tv_profile_id,
                    item.item_id,
                    item.checksum,
                    content_id,
                    item.added_at,
                    item.title,
                    time.time(),
                ),
            )

    def mark_current_stale(self, album_id: str, tv_profile_id: str, source_item_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT content_id FROM photo_album_tv_items
                   WHERE album_id = ? AND tv_profile_id = ? AND source_item_id = ?""",
                (album_id, tv_profile_id, source_item_id),
            ).fetchone()
            if not row:
                return
            connection.execute(
                """INSERT OR IGNORE INTO photo_album_stale_items
                   (album_id, tv_profile_id, content_id, created_at) VALUES (?, ?, ?, ?)""",
                (album_id, tv_profile_id, row["content_id"], time.time()),
            )
            connection.execute(
                """DELETE FROM photo_album_tv_items
                   WHERE album_id = ? AND tv_profile_id = ? AND source_item_id = ?""",
                (album_id, tv_profile_id, source_item_id),
            )

    def clear_stale(self, album_id: str, tv_profile_id: str, content_ids: list[str]) -> None:
        if not content_ids:
            return
        placeholders = ",".join("?" for _ in content_ids)
        with self._connect() as connection:
            connection.execute(
                f"""DELETE FROM photo_album_stale_items
                    WHERE album_id = ? AND tv_profile_id = ?
                    AND content_id IN ({placeholders})""",
                [album_id, tv_profile_id, *content_ids],
            )

    def all_owned_ids(self, album_id: str, tv_profile_id: str) -> list[str]:
        current = [
            item["content_id"]
            for item in self.current_items(album_id, tv_profile_id).values()
        ]
        return list(dict.fromkeys([*current, *self.stale_ids(album_id, tv_profile_id)]))

    def delete_album(self, album_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM photo_album_feeds WHERE id = ?", (album_id,))
        return cursor.rowcount > 0

    def replace_tv_profile_ids(self, replacements: dict[str, str]) -> int:
        normalized = {old: new for old, new in replacements.items() if old and new and old != new}
        if not normalized:
            return 0
        updated = 0
        with self._connect() as connection:
            for old, new in normalized.items():
                cursor = connection.execute(
                    """UPDATE photo_album_feeds SET target_id = ?, updated_at = ?
                       WHERE target_type = 'tv' AND target_id = ?""",
                    (new, time.time(), old),
                )
                updated += cursor.rowcount
                for table in ("photo_album_tv_items", "photo_album_stale_items"):
                    connection.execute(
                        f"UPDATE OR REPLACE {table} SET tv_profile_id = ? WHERE tv_profile_id = ?",
                        (new, old),
                    )
        return updated


class LiveAlbumService:
    """Periodically reconcile each album's newest ten photos with its target TVs."""

    def __init__(self, settings_loader, *, loop_seconds: float = 7.0) -> None:
        self.settings_loader = settings_loader
        self.loop_seconds = loop_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="frameart-live-album", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(self.loop_seconds + 1, 2))

    def _loop(self) -> None:
        while not self._stop.wait(self.loop_seconds):
            self.tick()

    def tick(self, now: float | None = None) -> list[dict[str, Any]]:
        settings = self.settings_loader()
        try:
            store = LiveAlbumStore(settings.data_dir)
            due_ids = store.due_album_ids(now)
        except sqlite3.OperationalError:
            logger.warning("Live album scheduler deferred a database-busy tick")
            return []
        results = []
        for album_id in due_ids:
            try:
                results.append(self.sync_album(album_id))
            except Exception:
                logger.exception("Live album sync failed for %s", album_id)
        return results

    @staticmethod
    def _target_profile_ids(settings, album: dict[str, Any]) -> tuple[list[str], str | None]:
        if album["target_type"] == "tv":
            return [album["target_id"]], None
        group = AutomationStore(settings.data_dir).get_group(album["target_id"])
        if not group:
            return [], "Configured TV group no longer exists."
        return group["tv_profile_ids"], None

    @staticmethod
    def _drain_stale(store, album_id, profile_id, profile) -> str | None:
        from frameart.tv.controller import delete_art

        stale = store.stale_ids(album_id, profile_id)
        if not stale:
            return None
        try:
            deleted = delete_art(profile, stale)
        except Exception as exc:
            return str(exc)
        if not deleted:
            return "TV did not delete superseded album photos"
        store.clear_stale(album_id, profile_id, stale)
        return None

    @staticmethod
    def _cache_snapshot(
        store: LiveAlbumStore,
        album_id: str,
        provider: str,
        snapshot: AlbumSnapshot,
    ) -> dict[tuple[str, str], bytes]:
        """Cache one complete newest-ten snapshot before replacing the gallery."""
        cache_dir = _album_cache_directory(store.data_dir, album_id)
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        prepared: dict[tuple[str, str], bytes] = {}
        created: list[Path] = []
        try:
            for item in snapshot.items:
                cache_path = cache_dir / _album_cache_name(item)
                saved = store.source_item(album_id, item.item_id)
                reusable = bool(
                    saved
                    and saved["source_checksum"] == item.checksum
                    and saved["cache_path"] == cache_path
                    and cache_path.is_file()
                )
                if reusable:
                    continue
                image_bytes = download_album_image(provider, item)
                temp_path = cache_dir / f".{cache_path.name}.{uuid.uuid4().hex}.tmp"
                try:
                    temp_path.write_bytes(image_bytes)
                    os.chmod(temp_path, 0o600)
                    existed = cache_path.exists()
                    os.replace(temp_path, cache_path)
                    if not existed:
                        created.append(cache_path)
                finally:
                    temp_path.unlink(missing_ok=True)
                prepared[(item.item_id, item.checksum)] = image_bytes
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise

        store.replace_source_items(album_id, snapshot.items)
        keep = {_album_cache_name(item) for item in snapshot.items}
        for path in cache_dir.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink(missing_ok=True)
        return prepared

    @staticmethod
    def _cached_image_bytes(
        store: LiveAlbumStore,
        album_id: str,
        item: AlbumItem,
        prepared: dict[tuple[str, str], bytes] | None = None,
    ) -> bytes:
        key = (item.item_id, item.checksum)
        if prepared and key in prepared:
            return prepared[key]
        saved = store.source_item(album_id, item.item_id)
        if saved is None or saved["source_checksum"] != item.checksum:
            raise RuntimeError("The album photo is not available in the local preview cache.")
        try:
            return saved["cache_path"].read_bytes()
        except OSError as exc:
            raise RuntimeError("The cached album photo is unavailable; synchronize again.") from exc

    def sync_album(self, album_id: str) -> dict[str, Any]:
        """Fetch one album and reconcile only the TV content owned by this feed."""
        with self._lock:
            settings = self.settings_loader()
            store = LiveAlbumStore(settings.data_dir)
            album = store.get_album(album_id, include_source=True)
            if album is None:
                raise KeyError(album_id)
            now = time.time()
            try:
                snapshot = fetch_album_items(album["provider"], album["source_url"])
                snapshot = AlbumSnapshot(
                    snapshot.name,
                    snapshot.total_count,
                    tuple(snapshot.items[:MAX_SYNC_PHOTOS]),
                )
                prepared = self._cache_snapshot(
                    store, album_id, album["provider"], snapshot
                )
            except Exception as exc:
                store.update_runtime(
                    album_id,
                    next_sync=now + album["interval_seconds"],
                    status="error",
                    error=str(exc),
                )
                IntegrationPublisher(AutomationStore(settings.data_dir)).publish(
                    "live_album.error", {"album_id": album_id, "error": str(exc)}
                )
                raise

            desired = {item.item_id: item for item in snapshot.items}
            target_ids, target_error = self._target_profile_ids(settings, album)
            all_profile_ids = list(
                dict.fromkeys([*target_ids, *store.owned_profile_ids(album_id)])
            )
            errors: list[str] = [target_error] if target_error else []
            results: list[dict[str, Any]] = []
            total_uploaded = 0
            total_deleted = 0
            target_synced_counts: dict[str, int] = {}

            from frameart.tv.controller import switch_art, upload_image

            for profile_id in all_profile_ids:
                profile = settings.tvs.get(profile_id)
                if profile is None:
                    errors.append(f"{profile_id}: TV profile is no longer configured")
                    continue
                stale_error = self._drain_stale(store, album_id, profile_id, profile)
                if stale_error:
                    errors.append(f"{profile_id}: {stale_error}; sync paused to bound TV storage")
                    continue
                is_target = profile_id in target_ids
                profile_desired = desired if is_target else {}
                current = store.current_items(album_id, profile_id)
                uploaded = 0
                profile_errors: list[str] = []
                for item in profile_desired.values():
                    saved = current.get(item.item_id)
                    if saved and saved["source_checksum"] == item.checksum:
                        continue
                    try:
                        image_bytes = self._cached_image_bytes(
                            store, album_id, item, prepared
                        )
                        result = upload_image(
                            profile, image_bytes, file_type="JPEG", matte="none"
                        )
                        if not result.success or not result.content_id:
                            raise RuntimeError(result.error or "TV upload failed")
                        store.upsert_current(
                            album_id, profile_id, item, result.content_id
                        )
                        current = store.current_items(album_id, profile_id)
                        uploaded += 1
                        total_uploaded += 1
                    except Exception as exc:
                        profile_errors.append(f"photo {item.item_id[:12]}: {exc}")

                ready = all(
                    item_id in current
                    and current[item_id]["source_checksum"] == item.checksum
                    for item_id, item in profile_desired.items()
                )
                if ready:
                    for item_id in list(current):
                        if item_id not in profile_desired:
                            store.mark_current_stale(album_id, profile_id, item_id)
                    stale_before = store.stale_ids(album_id, profile_id)
                    stale_error = self._drain_stale(store, album_id, profile_id, profile)
                    if stale_error:
                        profile_errors.append(stale_error)
                    else:
                        total_deleted += len(stale_before)
                    if is_target and uploaded and album["display_newest"] and snapshot.items:
                        newest = store.current_items(album_id, profile_id).get(
                            snapshot.items[0].item_id
                        )
                        if newest and not switch_art(
                            profile,
                            newest["content_id"],
                            require_art_mode=True,
                        ):
                            profile_errors.append(
                                "newest photo was not displayed because the TV was not "
                                "confirmed in Art Mode; normal viewing was left untouched"
                            )
                elif profile_errors:
                    profile_errors.append(
                        "older photos were kept until every replacement uploads successfully"
                    )
                results.append(
                    {
                        "tv_profile_id": profile_id,
                        "uploaded": uploaded,
                        "owned": len(store.current_items(album_id, profile_id)),
                    }
                )
                if is_target:
                    final_current = store.current_items(album_id, profile_id)
                    target_synced_counts[profile_id] = sum(
                        item_id in final_current
                        and final_current[item_id]["source_checksum"] == item.checksum
                        for item_id, item in desired.items()
                    )
                errors.extend(f"{profile_id}: {error}" for error in profile_errors)

            synced_count = (
                min(target_synced_counts.get(profile_id, 0) for profile_id in target_ids)
                if target_ids
                else 0
            )
            changed = bool(total_uploaded or total_deleted)
            if errors:
                status = "partial" if results else "error"
            else:
                status = "synced" if changed else "unchanged"
            error = "; ".join(error for error in errors if error) or None
            store.update_runtime(
                album_id,
                next_sync=now + album["interval_seconds"],
                status=status,
                error=error,
                source_name=snapshot.name,
                source_count=snapshot.total_count,
                synced_count=synced_count,
            )
            payload = {
                "album_id": album_id,
                "status": status,
                "source_count": snapshot.total_count,
                "synced_count": synced_count,
                "uploaded": total_uploaded,
                "deleted": total_deleted,
                "results": results,
                "errors": errors,
            }
            IntegrationPublisher(AutomationStore(settings.data_dir)).publish(
                f"live_album.{status}", payload
            )
            return payload

    def display_photo(self, album_id: str, item_id: str) -> dict[str, Any]:
        """Display one cached album photo without ever enabling Art Mode."""
        with self._lock:
            settings = self.settings_loader()
            store = LiveAlbumStore(settings.data_dir)
            album = store.get_album(album_id, include_source=True)
            if album is None:
                raise KeyError(album_id)
            saved_item = store.source_item(album_id, item_id)
            if saved_item is None:
                raise LookupError(item_id)
            try:
                image_bytes = saved_item["cache_path"].read_bytes()
            except OSError as exc:
                raise RuntimeError(
                    "The cached album photo is unavailable; synchronize the album again."
                ) from exc

            item = AlbumItem(
                item_id=saved_item["source_item_id"],
                image_url="",
                added_at=saved_item["added_at"],
                checksum=saved_item["source_checksum"],
                title=saved_item["title"],
            )
            profile_ids, target_error = self._target_profile_ids(settings, album)
            results: list[dict[str, str]] = []
            skipped: list[dict[str, str]] = []
            errors: list[str] = [target_error] if target_error else []

            from frameart.tv.controller import get_status, switch_art, upload_image

            for profile_id in profile_ids:
                profile = settings.tvs.get(profile_id)
                if profile is None:
                    errors.append(f"{profile_id}: TV profile is no longer configured")
                    continue
                try:
                    tv_status = get_status(profile)
                except Exception as exc:
                    skipped.append(
                        {
                            "tv_profile_id": profile_id,
                            "reason": f"Could not confirm Art Mode: {exc}",
                        }
                    )
                    continue
                if not (
                    tv_status.reachable
                    and tv_status.art_mode_supported
                    and tv_status.art_mode_on
                ):
                    skipped.append(
                        {
                            "tv_profile_id": profile_id,
                            "reason": (
                                "TV is not in Art Mode; normal viewing was left untouched."
                            ),
                        }
                    )
                    continue

                current = store.current_items(album_id, profile_id)
                existing = current.get(item_id)
                content_id = (
                    existing["content_id"]
                    if existing and existing["source_checksum"] == item.checksum
                    else None
                )
                try:
                    if content_id is None:
                        uploaded = upload_image(
                            profile, image_bytes, file_type="JPEG", matte="none"
                        )
                        if not uploaded.success or not uploaded.content_id:
                            raise RuntimeError(uploaded.error or "TV upload failed")
                        content_id = uploaded.content_id
                        store.upsert_current(album_id, profile_id, item, content_id)
                    if not switch_art(
                        profile, content_id, require_art_mode=True
                    ):
                        skipped.append(
                            {
                                "tv_profile_id": profile_id,
                                "reason": (
                                    "Art Mode changed before display; normal viewing was left "
                                    "untouched."
                                ),
                            }
                        )
                        continue
                    results.append(
                        {"tv_profile_id": profile_id, "content_id": content_id}
                    )
                except Exception as exc:
                    errors.append(f"{profile_id}: {exc}")

            if results and not skipped and not errors:
                status = "displayed"
            elif results:
                status = "partial"
            elif skipped and not errors:
                status = "skipped"
            else:
                status = "error"
            payload = {
                "album_id": album_id,
                "item_id": item_id,
                "status": status,
                "results": results,
                "skipped": skipped,
                "errors": errors,
            }
            IntegrationPublisher(AutomationStore(settings.data_dir)).publish(
                f"live_album.{status}", payload
            )
            return payload

    def delete_album(self, album_id: str) -> bool:
        """Delete a feed after removing every TV content ID that it owns."""
        settings = self.settings_loader()
        store = LiveAlbumStore(settings.data_dir)
        album = store.get_album(album_id, include_source=True)
        if album is None:
            return False
        from frameart.tv.controller import delete_art

        failures = []
        for profile_id in store.owned_profile_ids(album_id):
            content_ids = store.all_owned_ids(album_id, profile_id)
            profile = settings.tvs.get(profile_id)
            if profile is None:
                failures.append(f"{profile_id}: TV profile is no longer configured")
                continue
            if content_ids:
                try:
                    if not delete_art(profile, content_ids):
                        failures.append(f"{profile_id}: TV did not delete album photos")
                except Exception as exc:
                    failures.append(f"{profile_id}: {exc}")
        if failures:
            raise RuntimeError("; ".join(failures))
        deleted = store.delete_album(album_id)
        if deleted:
            try:
                store.clear_source_cache(album_id)
            except OSError:
                logger.warning("Could not remove cached previews for live album %s", album_id)
        return deleted
