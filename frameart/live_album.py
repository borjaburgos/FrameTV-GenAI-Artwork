"""Public live-album adapters, slideshow scheduling, and bounded storage."""

from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import os
import random
import re
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx2 as httpx
from PIL import Image, UnidentifiedImageError

from frameart.automation import AutomationStore, IntegrationPublisher
from frameart.display_modes import delete_mode_tv_images, replace_group_image
from frameart.postprocess import postprocess
from frameart.upscalers.none_upscaler import NoneUpscaler

logger = logging.getLogger(__name__)

_MAX_SOURCE_BYTES = 5 * 1024 * 1024
_MAX_IMAGE_BYTES = 30 * 1024 * 1024
_MAX_IMAGE_PIXELS = 50_000_000
_IMAGE_URL_RE = re.compile(
    r"https?:\\?/\\?/[^\"'<>\s]+?\.(?:jpe?g|png|webp|heic|heif)"
    r"(?:\?[^\"'<>\s]*)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AlbumItem:
    """One provider-neutral, remotely hosted slideshow image."""

    item_id: str
    image_url: str
    title: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    fallback_url: str | None = None


def _clean_absolute_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Source and image URLs must be absolute HTTP(S) URLs.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not supported.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def validate_source_url(value: str) -> str:
    """Validate URL structure without making a network request."""
    return _clean_absolute_url(value)


def _validate_network_target(value: str, *, allow_private_network: bool) -> str:
    clean = _clean_absolute_url(value)
    if allow_private_network:
        return clean
    parsed = urlsplit(clean)
    try:
        addresses = [ip_address(parsed.hostname or "")]
    except ValueError:
        try:
            records = socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise RuntimeError(f"Could not resolve remote host {parsed.hostname}.") from exc
        addresses = list({ip_address(record[4][0]) for record in records})
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError(
            "The source resolves to a private or non-public address. "
            "Enable private-network access only for a trusted LAN album server."
        )
    return clean


def _get_bytes(
    url: str,
    *,
    allow_private_network: bool,
    limit: int,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, str, str]:
    current_url = _validate_network_target(url, allow_private_network=allow_private_network)
    request_headers = {
        "Accept": "*/*",
        "User-Agent": "FrameArt/0.1",
        **(headers or {}),
    }
    response = None
    for _ in range(6):
        try:
            response = httpx.get(
                current_url,
                headers=request_headers,
                follow_redirects=False,
                timeout=20,
            )
        except Exception as exc:
            raise RuntimeError("Could not fetch the remote album source.") from exc
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        location = response.headers.get("location")
        if not location:
            raise RuntimeError("Remote album server returned an invalid redirect.")
        redirected = _validate_network_target(
            urljoin(current_url, location),
            allow_private_network=allow_private_network,
        )
        if urlsplit(redirected).netloc != urlsplit(current_url).netloc:
            request_headers = {
                key: value
                for key, value in request_headers.items()
                if not key.lower().startswith("x-immich-share-")
            }
        current_url = redirected
    else:
        raise RuntimeError("Remote album server exceeded the five-redirect limit.")
    if response is None:  # pragma: no cover - loop always performs at least one request
        raise RuntimeError("Could not fetch the remote album source.")
    try:
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"Remote album server returned HTTP {response.status_code}.") from exc
    final_url = _validate_network_target(current_url, allow_private_network=allow_private_network)
    declared_size = response.headers.get("content-length")
    try:
        declared_too_large = bool(declared_size and int(declared_size) > limit)
    except ValueError:
        declared_too_large = False
    if declared_too_large:
        raise ValueError(f"Remote response exceeds the {limit // (1024 * 1024)} MB limit.")
    body = response.content
    if len(body) > limit:
        raise ValueError(f"Remote response exceeds the {limit // (1024 * 1024)} MB limit.")
    return body, response.headers.get("content-type", ""), final_url


def _manifest_items(payload: Any, base_url: str) -> list[AlbumItem]:
    if isinstance(payload, dict):
        values = payload.get("photos") or payload.get("items") or payload.get("images") or []
    else:
        values = payload
    if not isinstance(values, list):
        raise ValueError("Album manifest must contain a photos, items, or images array.")
    items: list[AlbumItem] = []
    for index, value in enumerate(values):
        if isinstance(value, str):
            image_url, title, item_id = value, "", ""
        elif isinstance(value, dict):
            image_url = value.get("url") or value.get("image_url") or value.get("src")
            title = str(value.get("title") or value.get("caption") or "")
            item_id = str(value.get("id") or "")
        else:
            continue
        if not image_url:
            continue
        resolved = urljoin(base_url, str(image_url))
        clean = _clean_absolute_url(resolved)
        stable_id = item_id or hashlib.sha256(clean.encode()).hexdigest()[:24]
        items.append(AlbumItem(stable_id, clean, title[:300] or f"Photo {index + 1}"))
    return _unique_items(items)


class _AlbumHTMLParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value}
        if tag.lower() == "meta" and values.get("property", "").lower() in {
            "og:image", "og:image:url", "twitter:image"
        }:
            self.urls.append(urljoin(self.base_url, values.get("content", "")))
        if tag.lower() == "img":
            source = values.get("src") or values.get("data-src") or values.get("data-original")
            if source:
                self.urls.append(urljoin(self.base_url, source))
            if values.get("srcset"):
                candidates = [part.strip().split()[0] for part in values["srcset"].split(",")]
                if candidates:
                    self.urls.append(urljoin(self.base_url, candidates[-1]))


def extract_page_items(page: str, base_url: str) -> list[AlbumItem]:
    """Extract public image references from HTML and embedded page data."""
    parser = _AlbumHTMLParser(base_url)
    parser.feed(page)
    urls = list(parser.urls)
    for match in _IMAGE_URL_RE.findall(page):
        urls.append(html.unescape(match.replace("\\/", "/")))
    items = []
    for value in urls:
        try:
            clean = _clean_absolute_url(value)
        except ValueError:
            continue
        item_id = hashlib.sha256(clean.encode()).hexdigest()[:24]
        items.append(AlbumItem(item_id, clean, "Shared album photo"))
    return _unique_items(items)


def _unique_items(items: list[AlbumItem]) -> list[AlbumItem]:
    found: list[AlbumItem] = []
    seen: set[str] = set()
    for item in items:
        if item.image_url not in seen:
            seen.add(item.image_url)
            found.append(item)
    return found[:5000]


def _immich_source(source_url: str) -> tuple[str, list[tuple[str, str]]]:
    parsed = urlsplit(_clean_absolute_url(source_url))
    query = parse_qs(parsed.query)
    segments = [segment for segment in parsed.path.split("/") if segment]
    token = (query.get("key") or query.get("slug") or [None])[0]
    share_index = segments.index("share") if "share" in segments else -1
    if not token and share_index >= 0 and len(segments) > share_index + 1:
        token = segments[share_index + 1]
    if not token:
        raise ValueError("Immich source must be a public /share/{key-or-slug} URL.")
    prefix = "/" + "/".join(segments[:share_index]) if share_index > 0 else ""
    api_base = urlunsplit((parsed.scheme, parsed.netloc, prefix + "/api", "", ""))
    if query.get("slug"):
        candidates = [("x-immich-share-slug", token)]
    elif query.get("key"):
        candidates = [("x-immich-share-key", token)]
    else:
        candidates = [
            ("x-immich-share-key", token),
            ("x-immich-share-slug", token),
        ]
    return api_base, candidates


def load_album_items(
    provider: str,
    source_url: str,
    *,
    allow_private_network: bool,
) -> list[AlbumItem]:
    """Load and normalize an album's current remote item list."""
    if provider == "manifest":
        body, _, final_url = _get_bytes(
            source_url,
            allow_private_network=allow_private_network,
            limit=_MAX_SOURCE_BYTES,
            headers={"Accept": "application/json"},
        )
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Album manifest did not return valid JSON.") from exc
        items = _manifest_items(payload, final_url)
    elif provider == "public_page":
        body, _, final_url = _get_bytes(
            source_url,
            allow_private_network=allow_private_network,
            limit=_MAX_SOURCE_BYTES,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        items = extract_page_items(body.decode("utf-8", errors="replace"), final_url)
    elif provider == "immich":
        api_base, candidates = _immich_source(source_url)
        payload = None
        auth_header: tuple[str, str] | None = None
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                body, _, _ = _get_bytes(
                    api_base + "/shared-links/me",
                    allow_private_network=allow_private_network,
                    limit=_MAX_SOURCE_BYTES,
                    headers={candidate[0]: candidate[1], "Accept": "application/json"},
                )
                payload = json.loads(body)
                auth_header = candidate
                break
            except Exception as exc:
                last_error = exc
        if payload is None or auth_header is None:
            raise RuntimeError("Could not open the Immich public share.") from last_error
        rows = (payload.get("album") or {}).get("assets") or payload.get("assets") or []
        items = []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("type", "IMAGE")).upper() != "IMAGE":
                continue
            asset_id = str(row.get("id") or "")
            if not asset_id:
                continue
            title = str(row.get("originalFileName") or row.get("fileCreatedAt") or "Photo")
            items.append(
                AlbumItem(
                    asset_id,
                    f"{api_base}/assets/{asset_id}/original",
                    title[:300],
                    {auth_header[0]: auth_header[1]},
                    f"{api_base}/assets/{asset_id}/thumbnail?size=preview",
                )
            )
    else:
        raise ValueError(f"Unsupported live-album provider: {provider}")
    if not items:
        raise RuntimeError("The public album did not expose any supported still images.")
    return items


def download_album_image(
    item: AlbumItem,
    *,
    allow_private_network: bool,
) -> bytes:
    """Download and verify one still image within strict size limits."""
    try:
        body, content_type, _ = _get_bytes(
            item.image_url,
            allow_private_network=allow_private_network,
            limit=_MAX_IMAGE_BYTES,
            headers=item.headers,
        )
    except Exception:
        if not item.fallback_url:
            raise
        body, content_type, _ = _get_bytes(
            item.fallback_url,
            allow_private_network=allow_private_network,
            limit=_MAX_IMAGE_BYTES,
            headers=item.headers,
        )
    if content_type and not (
        content_type.lower().startswith("image/")
        or content_type.lower().startswith("application/octet-stream")
    ):
        raise ValueError("Remote album item did not return an image content type.")
    try:
        with Image.open(io.BytesIO(body)) as image:
            if image.width * image.height > _MAX_IMAGE_PIXELS:
                raise ValueError("Remote image exceeds the 50-megapixel safety limit.")
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Remote album item is not a valid supported still image.") from exc
    return body


class LiveAlbumStore:
    """Persist live-album configuration and bounded runtime state."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.database_path = self.data_dir / "frameart.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_albums (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    provider TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    shuffle INTEGER NOT NULL,
                    allow_private_network INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    next_advance REAL NOT NULL,
                    last_run REAL,
                    last_status TEXT,
                    last_error TEXT,
                    last_item_id TEXT,
                    last_item_title TEXT,
                    source_count INTEGER,
                    current_index INTEGER NOT NULL,
                    current_content_ids TEXT NOT NULL,
                    stale_content_ids TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_albums_due
                    ON live_albums(enabled, next_advance);
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
    def _decode(row: sqlite3.Row, *, include_source: bool = False) -> dict[str, Any]:
        item = dict(row)
        source_url = item.pop("source_url")
        parsed = urlsplit(source_url)
        item["source_host"] = parsed.hostname
        item["has_source_url"] = bool(source_url)
        if include_source:
            item["source_url"] = source_url
        item["enabled"] = bool(item["enabled"])
        item["shuffle"] = bool(item["shuffle"])
        item["allow_private_network"] = bool(item["allow_private_network"])
        item["current_content_ids"] = json.loads(item["current_content_ids"] or "{}")
        item["stale_content_ids"] = json.loads(item["stale_content_ids"] or "{}")
        return item

    def list_albums(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM live_albums ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def get_album(self, album_id: str, *, include_source: bool = False):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM live_albums WHERE id = ?", (album_id,)
            ).fetchone()
        return self._decode(row, include_source=include_source) if row else None

    def create_album(
        self,
        *,
        name: str,
        provider: str,
        source_url: str,
        group_id: str,
        interval_seconds: int,
        shuffle: bool,
        allow_private_network: bool,
        enabled: bool,
    ) -> dict[str, Any]:
        now = time.time()
        album_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO live_albums (
                    id, name, provider, source_url, group_id, interval_seconds,
                    shuffle, allow_private_network, enabled, next_advance,
                    current_index, current_content_ids, stale_content_ids, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, -1, '{}', '{}', ?)
                """,
                (
                    album_id, name.strip(), provider, source_url, group_id,
                    interval_seconds, int(shuffle), int(allow_private_network), int(enabled),
                    now + interval_seconds, now,
                ),
            )
        return self.get_album(album_id) or {"id": album_id, "name": name}

    def delete_album(self, album_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM live_albums WHERE id = ?", (album_id,))
        return cursor.rowcount > 0

    def set_enabled(self, album_id: str, enabled: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE live_albums SET enabled = ?, next_advance = ? WHERE id = ?",
                (int(enabled), time.time(), album_id),
            )
        return cursor.rowcount > 0

    def due_album_ids(self, now: float | None = None) -> list[str]:
        cutoff = time.time() if now is None else now
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id FROM live_albums
                   WHERE enabled = 1 AND next_advance <= ?
                   ORDER BY next_advance LIMIT 20""",
                (cutoff,),
            ).fetchall()
        return [row["id"] for row in rows]

    def update_runtime(
        self,
        album_id: str,
        *,
        next_advance: float,
        last_status: str,
        last_error: str | None,
        last_item_id: str | None = None,
        last_item_title: str | None = None,
        source_count: int | None = None,
        current_index: int | None = None,
        current_content_ids: dict[str, str] | None = None,
        stale_content_ids: dict[str, list[str]] | None = None,
    ) -> None:
        assignments = [
            "next_advance = ?", "last_run = ?", "last_status = ?", "last_error = ?"
        ]
        values: list[Any] = [next_advance, time.time(), last_status, last_error]
        optional: dict[str, Any] = {
            "last_item_id": last_item_id,
            "last_item_title": last_item_title,
            "source_count": source_count,
            "current_index": current_index,
            "current_content_ids": (
                json.dumps(current_content_ids) if current_content_ids is not None else None
            ),
            "stale_content_ids": (
                json.dumps(stale_content_ids) if stale_content_ids is not None else None
            ),
        }
        for field_name, value in optional.items():
            if value is not None:
                assignments.append(f"{field_name} = ?")
                values.append(value)
        values.append(album_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE live_albums SET {', '.join(assignments)} WHERE id = ?", values
            )


class LiveAlbumService:
    """Advance public slideshows and retain only their current generated state."""

    def __init__(self, settings_loader, *, loop_seconds: float = 5.0) -> None:
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
        store = LiveAlbumStore(settings.data_dir)
        results = []
        for album_id in store.due_album_ids(now):
            try:
                results.append(self.advance_album(album_id))
            except Exception:
                logger.exception("Live album advance failed for %s", album_id)
        return results

    def advance_album(self, album_id: str) -> dict[str, Any]:
        with self._lock:
            settings = self.settings_loader()
            store = LiveAlbumStore(settings.data_dir)
            album = store.get_album(album_id, include_source=True)
            if album is None:
                raise KeyError(album_id)
            now = time.time()
            try:
                items = load_album_items(
                    album["provider"], album["source_url"],
                    allow_private_network=album["allow_private_network"],
                )
                if album["shuffle"]:
                    random.Random(f"{album_id}:{int(now // 86400)}").shuffle(items)
                index = (int(album["current_index"]) + 1) % len(items)
                if len(items) > 1 and items[index].item_id == album.get("last_item_id"):
                    index = (index + 1) % len(items)
                item = items[index]
                source_bytes = download_album_image(
                    item, allow_private_network=album["allow_private_network"]
                )
                processed = postprocess(source_bytes, NoneUpscaler())
                image_path = (
                    Path(settings.data_dir) / "modes" / "live-album" / album_id / "current.png"
                )
                image_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = image_path.with_suffix(".tmp.png")
                temporary.write_bytes(processed.image_bytes)
                os.chmod(temporary, 0o600)
                os.replace(temporary, image_path)
                current, stale, display_results, display_errors = replace_group_image(
                    settings, album, image_path
                )
                status = (
                    "displayed" if display_results and not display_errors
                    else ("partial" if display_results else "error")
                )
                error = "; ".join(display_errors) or None
                store.update_runtime(
                    album_id,
                    next_advance=now + album["interval_seconds"],
                    last_status=status,
                    last_error=error,
                    last_item_id=item.item_id,
                    last_item_title=item.title,
                    source_count=len(items),
                    current_index=index,
                    current_content_ids=current,
                    stale_content_ids=stale,
                )
                payload = {
                    "album_id": album_id,
                    "status": status,
                    "item": {"id": item.item_id, "title": item.title},
                    "source_count": len(items),
                    "results": display_results,
                    "errors": display_errors,
                }
                IntegrationPublisher(AutomationStore(settings.data_dir)).publish(
                    f"live_album.{status}", payload
                )
                return payload
            except Exception as exc:
                store.update_runtime(
                    album_id,
                    next_advance=now + album["interval_seconds"],
                    last_status="error",
                    last_error=str(exc),
                )
                payload = {"album_id": album_id, "status": "error", "error": str(exc)}
                IntegrationPublisher(AutomationStore(settings.data_dir)).publish(
                    "live_album.error", payload
                )
                raise

    def delete_album(self, album_id: str) -> bool:
        settings = self.settings_loader()
        store = LiveAlbumStore(settings.data_dir)
        album = store.get_album(album_id, include_source=True)
        if album is None:
            return False
        delete_mode_tv_images(settings, album)
        deleted = store.delete_album(album_id)
        mode_dir = Path(settings.data_dir) / "modes" / "live-album" / album_id
        if mode_dir.is_dir():
            shutil.rmtree(mode_dir)
        return deleted
