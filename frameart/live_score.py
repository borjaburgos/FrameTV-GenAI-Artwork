"""Live score tracking, deterministic scoreboard rendering, and bounded TV storage."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx2 as httpx
from PIL import Image, ImageDraw, ImageFont

from frameart.automation import AutomationStore, IntegrationPublisher
from frameart.settings_store import read_integration_secrets

logger = logging.getLogger(__name__)

_SPORTSDB_BASE_URL = "https://www.thesportsdb.com/api/v2/json"
_SPORTSDB_INTEGRATION_NAME = "thesportsdb"
_SPORTSDB_ENVIRONMENT_KEYS = ("FRAMEART_THESPORTSDB_API_KEY", "THESPORTSDB_API_KEY")
_TEAM_LOGO_CACHE_MAX_BYTES = 64 * 1024 * 1024
_TEAM_LOGO_CACHE_MAX_ENTRIES = 128
_TEAM_LOGO_DOWNLOAD_MAX_BYTES = 5 * 1024 * 1024
_TEAM_LOGO_MAX_DIMENSION = 4096
_TEAM_LOGO_RENDER_SIZE = 360
_TEAM_LOGO_LOOKUP_FAILURE_TTL_SECONDS = 300
_TEAM_LOGO_LOOKUP_TTL_SECONDS = 86_400
_NON_ACTIVE_EVENT_STATUSES = {
    "NS",
    "NOT STARTED",
    "TBD",
    "SCHEDULED",
    "FT",
    "AET",
    "FINAL",
    "FINISHED",
    "COMPLETE",
    "COMPLETED",
    "PST",
    "POSTPONED",
    "CANC",
    "CANCELLED",
    "ABD",
    "ABANDONED",
    "AWD",
    "WO",
    "SUSP",
    "SUSPENDED",
    "INT",
    "INTERRUPTED",
    "DELAYED",
}


def sportsdb_api_key(data_dir: Path, tracker_key: str | None = None) -> str | None:
    """Resolve the shared SportsDB key while retaining per-tracker compatibility."""
    for name in _SPORTSDB_ENVIRONMENT_KEYS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    managed = read_integration_secrets(data_dir).get(_SPORTSDB_INTEGRATION_NAME, "").strip()
    return managed or (tracker_key.strip() if tracker_key else None)


def sportsdb_api_key_source(data_dir: Path) -> str | None:
    """Return the active shared SportsDB key source without exposing the key."""
    if any(os.environ.get(name, "").strip() for name in _SPORTSDB_ENVIRONMENT_KEYS):
        return "environment"
    if read_integration_secrets(data_dir).get(_SPORTSDB_INTEGRATION_NAME, "").strip():
        return "managed"
    return None


@dataclass
class ScoreEvent:
    """Provider-neutral live event state used by the renderer."""

    event_id: str
    league: str
    sport: str
    home_team: str
    away_team: str
    home_score: str = "-"
    away_score: str = "-"
    status: str = "NS"
    progress: str = "Not started"
    start_time: str | None = None
    home_team_id: str | None = None
    away_team_id: str | None = None
    league_id: str | None = None
    home_logo_url: str | None = None
    away_logo_url: str | None = None
    highlights: list[str] = field(default_factory=list)
    provider_updated_at: str | None = None

    def digest(self) -> str:
        stable = asdict(self)
        stable.pop("provider_updated_at", None)
        encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class LiveScoreStore:
    """Persist tracker configuration and runtime state in the shared database."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.database_path = self.data_dir / "frameart.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_score_trackers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    provider TEXT NOT NULL,
                    api_key TEXT,
                    tracking_kind TEXT NOT NULL,
                    tracking_value TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    target_type TEXT NOT NULL DEFAULT 'group',
                    target_id TEXT NOT NULL,
                    poll_seconds INTEGER NOT NULL,
                    refresh_seconds INTEGER NOT NULL,
                    theme TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    next_poll REAL NOT NULL,
                    last_polled REAL,
                    last_rendered REAL,
                    last_digest TEXT,
                    last_status TEXT,
                    last_error TEXT,
                    last_event TEXT,
                    current_content_ids TEXT NOT NULL,
                    stale_content_ids TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_score_due
                    ON live_score_trackers(enabled, next_poll);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(live_score_trackers)")
            }
            if "target_type" not in columns:
                connection.execute(
                    "ALTER TABLE live_score_trackers "
                    "ADD COLUMN target_type TEXT NOT NULL DEFAULT 'group'"
                )
            if "target_id" not in columns:
                connection.execute("ALTER TABLE live_score_trackers ADD COLUMN target_id TEXT")
            connection.execute(
                "UPDATE live_score_trackers SET target_id = group_id WHERE target_id IS NULL"
            )
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row, *, include_secret: bool = False) -> dict[str, Any]:
        item = dict(row)
        api_key = item.pop("api_key", None)
        item["has_api_key"] = bool(api_key)
        if include_secret:
            item["api_key"] = api_key
        item["enabled"] = bool(item["enabled"])
        target_type = item.get("target_type") or "group"
        target_id = item.get("target_id") or item["group_id"]
        item["target_type"] = target_type
        item["target_id"] = target_id
        item["group_id"] = target_id if target_type == "group" else None
        item["tv_profile_id"] = target_id if target_type == "tv" else None
        for field_name, fallback in (
            ("last_event", None),
            ("current_content_ids", {}),
            ("stale_content_ids", {}),
        ):
            raw = item.get(field_name)
            item[field_name] = json.loads(raw) if raw else fallback
        return item

    def list_trackers(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM live_score_trackers ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def get_tracker(
        self,
        tracker_id: str,
        *,
        include_secret: bool = False,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM live_score_trackers WHERE id = ?", (tracker_id,)
            ).fetchone()
        return self._decode(row, include_secret=include_secret) if row else None

    def create_tracker(
        self,
        *,
        name: str,
        provider: str,
        api_key: str | None,
        tracking_kind: str,
        tracking_value: str,
        poll_seconds: int,
        refresh_seconds: int,
        theme: str,
        enabled: bool,
        group_id: str | None = None,
        tv_profile_id: str | None = None,
    ) -> dict[str, Any]:
        if (group_id is None) == (tv_profile_id is None):
            raise ValueError("Choose exactly one live-score TV or TV group target.")
        target_type = "group" if group_id is not None else "tv"
        target_id = group_id or tv_profile_id
        assert target_id is not None
        now = time.time()
        values = {
            "id": uuid.uuid4().hex,
            "name": name.strip(),
            "provider": provider,
            "api_key": api_key,
            "tracking_kind": tracking_kind,
            "tracking_value": tracking_value.strip(),
            "group_id": group_id,
            "tv_profile_id": tv_profile_id,
            "target_type": target_type,
            "target_id": target_id,
            "poll_seconds": poll_seconds,
            "refresh_seconds": refresh_seconds,
            "theme": theme,
            "enabled": enabled,
            "next_poll": now,
            "created_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO live_score_trackers (
                    id, name, provider, api_key, tracking_kind, tracking_value,
                    group_id, target_type, target_id, poll_seconds, refresh_seconds, theme, enabled,
                    next_poll, current_content_ids, stale_content_ids, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?)
                """,
                (
                    values["id"], values["name"], provider, api_key, tracking_kind,
                    values["tracking_value"], target_id, target_type, target_id,
                    poll_seconds, refresh_seconds, theme, int(enabled), now, now,
                ),
            )
        return self.get_tracker(values["id"]) or values

    def replace_tv_profile_ids(self, replacements: dict[str, str]) -> int:
        """Rewrite direct-TV tracker targets after profile renames or consolidation."""
        normalized = {
            source: target
            for source, target in replacements.items()
            if source and target and source != target
        }
        if not normalized:
            return 0

        updated = 0
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, group_id, target_type, target_id,
                       current_content_ids, stale_content_ids
                FROM live_score_trackers
                """
            ).fetchall()
            for row in rows:
                target_id = row["target_id"] or row["group_id"]
                replacement = (
                    normalized.get(target_id)
                    if row["target_type"] == "tv"
                    else None
                )
                current_content_ids = json.loads(row["current_content_ids"] or "{}")
                stale_content_ids = json.loads(row["stale_content_ids"] or "{}")
                remapped_current: dict[str, str] = {}
                remapped_stale: dict[str, list[str]] = {}
                for profile_id, content_ids in stale_content_ids.items():
                    remapped_id = normalized.get(profile_id, profile_id)
                    remapped_stale.setdefault(remapped_id, []).extend(content_ids)
                for profile_id, content_id in current_content_ids.items():
                    remapped_id = normalized.get(profile_id, profile_id)
                    existing = remapped_current.get(remapped_id)
                    if existing and existing != content_id:
                        remapped_stale.setdefault(remapped_id, []).append(content_id)
                    else:
                        remapped_current[remapped_id] = content_id
                remapped_stale = {
                    profile_id: list(dict.fromkeys(content_ids))[-10:]
                    for profile_id, content_ids in remapped_stale.items()
                }
                maps_changed = (
                    remapped_current != current_content_ids
                    or remapped_stale != stale_content_ids
                )
                if replacement is None and not maps_changed:
                    continue
                connection.execute(
                    """
                    UPDATE live_score_trackers
                    SET group_id = ?, target_id = ?,
                        current_content_ids = ?, stale_content_ids = ?
                    WHERE id = ?
                    """,
                    (
                        replacement or row["group_id"],
                        replacement or target_id,
                        json.dumps(remapped_current),
                        json.dumps(remapped_stale),
                        row["id"],
                    ),
                )
                updated += 1
        return updated

    def delete_tracker(self, tracker_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM live_score_trackers WHERE id = ?", (tracker_id,)
            )
        return cursor.rowcount > 0

    def set_enabled(self, tracker_id: str, enabled: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE live_score_trackers SET enabled = ?, next_poll = ? WHERE id = ?",
                (int(enabled), time.time(), tracker_id),
            )
        return cursor.rowcount > 0

    def due_tracker_ids(self, now: float | None = None) -> list[str]:
        cutoff = time.time() if now is None else now
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM live_score_trackers
                WHERE enabled = 1 AND provider != 'manual' AND next_poll <= ?
                ORDER BY next_poll LIMIT 20
                """,
                (cutoff,),
            ).fetchall()
        return [row["id"] for row in rows]

    def update_runtime(
        self,
        tracker_id: str,
        *,
        next_poll: float,
        last_status: str,
        last_error: str | None,
        last_event: dict[str, Any] | None = None,
        last_digest: str | None = None,
        last_rendered: float | None = None,
        current_content_ids: dict[str, str] | None = None,
        stale_content_ids: dict[str, list[str]] | None = None,
    ) -> None:
        assignments = [
            "next_poll = ?",
            "last_polled = ?",
            "last_status = ?",
            "last_error = ?",
        ]
        values: list[Any] = [next_poll, time.time(), last_status, last_error]
        optional = {
            "last_event": json.dumps(last_event) if last_event is not None else None,
            "last_digest": last_digest,
            "last_rendered": last_rendered,
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
        values.append(tracker_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE live_score_trackers SET {', '.join(assignments)} WHERE id = ?",
                values,
            )


class TheSportsDBClient:
    """Premium TheSportsDB v2 livescore adapter."""

    def __init__(self, api_key: str, *, base_url: str = _SPORTSDB_BASE_URL) -> None:
        if not api_key:
            raise ValueError("TheSportsDB live scores require a premium API key.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._team_logo_urls: dict[str, tuple[float, str | None]] = {}

    def fetch(self, tracking_kind: str, tracking_value: str) -> ScoreEvent | None:
        if tracking_kind == "league" and tracking_value.isdigit():
            endpoint = f"/livescore/{tracking_value}"
        elif tracking_kind == "sport":
            endpoint = f"/livescore/{tracking_value.lower().replace(' ', '_')}"
        else:
            endpoint = "/livescore/all"
        response = httpx.get(
            self.base_url + endpoint,
            headers={"X-API-KEY": self.api_key, "Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        rows = self._payload_rows(payload)
        events = [self._normalize(row) for row in rows if isinstance(row, dict)]
        matches = [
            event
            for event in events
            if event_matches(event, tracking_kind, tracking_value)
        ]
        event = min(
            matches,
            key=self._event_live_priority,
            default=None,
        )
        if event is not None:
            self._resolve_missing_team_logos(event)
        return event

    @staticmethod
    def _payload_rows(payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("livescore", "livescores", "events", "data"):
            rows = payload.get(key)
            if isinstance(rows, list) and rows:
                return rows
        return []

    @staticmethod
    def _event_live_priority(event: ScoreEvent) -> int:
        """Prefer in-play/unknown statuses over terminal or not-started events."""
        normalized = event.status.strip().upper().replace("_", " ").replace("-", " ")
        normalized = " ".join(normalized.split())
        return int(normalized in _NON_ACTIVE_EVENT_STATUSES)

    def _resolve_missing_team_logos(self, event: ScoreEvent) -> None:
        """Fill missing livescore badge URLs without making logo failures fatal."""
        if not event.home_logo_url and event.home_team_id:
            event.home_logo_url = self._lookup_team_logo(event.home_team_id)
        if not event.away_logo_url and event.away_team_id:
            event.away_logo_url = self._lookup_team_logo(event.away_team_id)

    def _lookup_team_logo(self, team_id: str) -> str | None:
        cached = self._team_logo_urls.get(team_id)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]

        logo_url: str | None = None
        try:
            response = httpx.get(
                f"{self.base_url}/lookup/team/{team_id}",
                headers={"X-API-KEY": self.api_key, "Accept": "application/json"},
                timeout=10,
            )
            response.raise_for_status()
            for row in self._team_lookup_rows(response.json()):
                logo_url = self._text(
                    row,
                    "strBadge",
                    "strTeamBadge",
                    "strLogo",
                    "team_badge",
                    "badge",
                    "logo",
                ) or None
                if logo_url:
                    break
        except Exception as exc:
            logger.warning("Could not resolve TheSportsDB logo for team %s: %s", team_id, exc)
        ttl = (
            _TEAM_LOGO_LOOKUP_TTL_SECONDS
            if logo_url
            else _TEAM_LOGO_LOOKUP_FAILURE_TTL_SECONDS
        )
        self._team_logo_urls[team_id] = (time.monotonic() + ttl, logo_url)
        return logo_url

    @staticmethod
    def _team_lookup_rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("teams", "team", "lookup", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                return [value]
        if any(key in payload for key in ("strBadge", "strTeamBadge", "strLogo")):
            return [payload]
        return []

    @staticmethod
    def _text(row: dict[str, Any], *keys: str, default: str = "") -> str:
        for key in keys:
            value = row.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return default

    @staticmethod
    def _normalize(row: dict[str, Any]) -> ScoreEvent:
        def text(*keys: str, default: str = "") -> str:
            return TheSportsDBClient._text(row, *keys, default=default)

        progress = text("strProgress", "progress", "strStatus", "status", default="Live")
        status = text("strStatus", "status", default=progress)
        return ScoreEvent(
            event_id=text("idEvent", "event_id", "id"),
            league=text("strLeague", "league", default="Live event"),
            sport=text("strSport", "sport"),
            home_team=text("strHomeTeam", "home_team", default="Home"),
            away_team=text("strAwayTeam", "away_team", default="Away"),
            home_score=text("intHomeScore", "home_score", default="-"),
            away_score=text("intAwayScore", "away_score", default="-"),
            status=status,
            progress=progress,
            start_time=text("strEventTime", "start_time") or None,
            home_team_id=text("idHomeTeam", "home_team_id") or None,
            away_team_id=text("idAwayTeam", "away_team_id") or None,
            league_id=text("idLeague", "league_id") or None,
            home_logo_url=text(
                "strHomeTeamBadge",
                "strHomeBadge",
                "home_team_badge",
                "home_logo_url",
            ) or None,
            away_logo_url=text(
                "strAwayTeamBadge",
                "strAwayBadge",
                "away_team_badge",
                "away_logo_url",
            ) or None,
            highlights=[],
            provider_updated_at=text("updated", "updated_at") or None,
        )


def event_matches(event: ScoreEvent, tracking_kind: str, tracking_value: str) -> bool:
    needle = tracking_value.strip().lower()
    if tracking_kind == "game":
        return event.event_id.lower() == needle
    if tracking_kind == "league":
        return needle in {event.league_id.lower() if event.league_id else "", event.league.lower()}
    if tracking_kind == "sport":
        return event.sport.lower().replace(" ", "_") == needle.replace(" ", "_")
    if tracking_kind == "team":
        candidates = {
            event.home_team.lower(),
            event.away_team.lower(),
            event.home_team_id.lower() if event.home_team_id else "",
            event.away_team_id.lower() if event.away_team_id else "",
        }
        return needle in candidates
    return False


def _is_sportsdb_logo_url(value: str) -> bool:
    """Only fetch HTTPS artwork hosted by TheSportsDB."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and bool(parsed.path)
        and (hostname == "thesportsdb.com" or hostname.endswith(".thesportsdb.com"))
    )


def _read_cached_team_logo(cache_path: Path) -> Image.Image | None:
    if not cache_path.is_file():
        return None
    try:
        with Image.open(cache_path) as cached:
            width, height = cached.size
            if not width or not height or max(width, height) > _TEAM_LOGO_MAX_DIMENSION:
                raise ValueError("cached logo dimensions are invalid")
            cached.load()
            logo = cached.convert("RGBA")
        os.utime(cache_path, None)
        return logo
    except Exception:
        with contextlib.suppress(OSError):
            cache_path.unlink()
        return None


def _prune_team_logo_cache(cache_dir: Path) -> None:
    try:
        entries = sorted(
            (path for path in cache_dir.glob("*.png") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        total_bytes = sum(path.stat().st_size for path in entries)
        while entries and (
            len(entries) > _TEAM_LOGO_CACHE_MAX_ENTRIES
            or total_bytes > _TEAM_LOGO_CACHE_MAX_BYTES
        ):
            path = entries.pop(0)
            size = path.stat().st_size
            path.unlink()
            total_bytes -= size
    except OSError:
        logger.debug("Could not prune the live-score logo cache", exc_info=True)


def _load_team_logo(logo_url: str | None, cache_dir: Path | None) -> Image.Image | None:
    """Load and normalize a team logo, returning None for any unsafe or invalid input."""
    if not logo_url or not _is_sportsdb_logo_url(logo_url):
        return None

    cache_path: Path | None = None
    if cache_dir is not None:
        cache_key = hashlib.sha256(logo_url.encode()).hexdigest()
        cache_path = Path(cache_dir) / f"{cache_key}.png"
        cached = _read_cached_team_logo(cache_path)
        if cached is not None:
            return cached

    temporary: Path | None = None
    try:
        response = httpx.get(
            logo_url,
            headers={"Accept": "image/png,image/webp,image/jpeg"},
            timeout=8,
        )
        response.raise_for_status()
        content_type = str(response.headers.get("content-type", "")).split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            raise ValueError("logo response is not an image")
        content = bytes(response.content)
        if not content or len(content) > _TEAM_LOGO_DOWNLOAD_MAX_BYTES:
            raise ValueError("logo response size is invalid")
        with Image.open(BytesIO(content)) as source:
            width, height = source.size
            if not width or not height or max(width, height) > _TEAM_LOGO_MAX_DIMENSION:
                raise ValueError("logo dimensions are invalid")
            source.load()
            logo = source.convert("RGBA")
        logo.thumbnail(
            (_TEAM_LOGO_RENDER_SIZE, _TEAM_LOGO_RENDER_SIZE),
            Image.Resampling.LANCZOS,
        )
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(cache_path.parent, 0o700)
            temporary = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
            logo.save(temporary, format="PNG", optimize=True)
            os.chmod(temporary, 0o600)
            os.replace(temporary, cache_path)
            _prune_team_logo_cache(cache_path.parent)
        return logo
    except Exception as exc:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
        logger.warning("Could not load TheSportsDB team logo: %s", exc)
        return None


def _paste_team_logo(image: Image.Image, logo: Image.Image | None, center_x: int, top: int) -> None:
    if logo is None:
        return
    rendered = logo.convert("RGBA")
    rendered.thumbnail(
        (_TEAM_LOGO_RENDER_SIZE, _TEAM_LOGO_RENDER_SIZE),
        Image.Resampling.LANCZOS,
    )
    left = round(center_x - rendered.width / 2)
    image.paste(rendered, (left, top), rendered)


def _font(size: int, *, bold: bool = False):
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else
             "/usr/share/fonts/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/noto/NotoSans-Bold.ttf" if bold else
             "/usr/share/fonts/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else
             "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, bold=True):
    size = start_size
    while size > 48:
        font = _font(size, bold=bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
        size -= 8
    return _font(48, bold=bold)


def render_scoreboard(
    event: ScoreEvent,
    output_path: Path,
    *,
    theme: str = "dark",
    logo_cache_dir: Path | None = None,
) -> Path:
    """Render a TV-safe 4K still without calling a generative image provider."""
    palettes = {
        "dark": ("#07111f", "#101f35", "#f7fbff", "#60a5fa", "#8ea4bf"),
        "light": ("#eaf1f8", "#ffffff", "#11243d", "#2563eb", "#506985"),
        "stadium": ("#071a12", "#0d2b1c", "#f4fff8", "#4ade80", "#9bc7ac"),
    }
    background, panel, text_color, accent, muted = palettes.get(theme, palettes["dark"])
    image = Image.new("RGB", (3840, 2160), background)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((190, 160, 3650, 1995), radius=60, fill=panel)
    draw.rectangle((190, 160, 3650, 182), fill=accent)

    home_logo = _load_team_logo(event.home_logo_url, logo_cache_dir)
    away_logo = _load_team_logo(event.away_logo_url, logo_cache_dir)
    logo_layout = home_logo is not None or away_logo is not None

    league_font = _font(66, bold=True)
    status_font = _font(58, bold=True)
    team_font_home = _fit_text(draw, event.home_team, 1180, 112 if logo_layout else 128)
    team_font_away = _fit_text(draw, event.away_team, 1180, 112 if logo_layout else 128)
    score_font = _font(270, bold=True)
    progress_font = _font(82, bold=True)
    highlight_font = _font(52)
    small_font = _font(42)

    draw.text(
        (280, 255),
        event.league or event.sport or "Live Score",
        fill=accent,
        font=league_font,
    )
    status_label = event.progress or event.status or "Live"
    status_box = draw.textbbox((0, 0), status_label, font=status_font)
    draw.text((3560 - status_box[2], 255), status_label, fill=text_color, font=status_font)

    if logo_layout:
        _paste_team_logo(image, home_logo, 980, 450)
        _paste_team_logo(image, away_logo, 2860, 450)
        home_team_box = draw.textbbox((0, 0), event.home_team, font=team_font_home)
        away_team_box = draw.textbbox((0, 0), event.away_team, font=team_font_away)
        draw.text(
            (980 - home_team_box[2] / 2, 830),
            event.home_team,
            fill=text_color,
            font=team_font_home,
        )
        draw.text(
            (2860 - away_team_box[2] / 2, 830),
            event.away_team,
            fill=text_color,
            font=team_font_away,
        )
        score_y = 1015
        progress_y = 1355
    else:
        draw.text((330, 650), event.home_team, fill=text_color, font=team_font_home)
        away_box = draw.textbbox((0, 0), event.away_team, font=team_font_away)
        draw.text((3510 - away_box[2], 650), event.away_team, fill=text_color, font=team_font_away)
        score_y = 910
        progress_y = 1325

    home_score_box = draw.textbbox((0, 0), event.home_score, font=score_font)
    away_score_box = draw.textbbox((0, 0), event.away_score, font=score_font)
    draw.text(
        (980 - home_score_box[2] / 2, score_y),
        event.home_score,
        fill=text_color,
        font=score_font,
    )
    draw.text(
        (2860 - away_score_box[2] / 2, score_y),
        event.away_score,
        fill=text_color,
        font=score_font,
    )
    draw.text((1835, score_y + 45), "-", fill=accent, font=score_font)

    progress = event.status if event.status != status_label else "LIVE"
    progress_box = draw.textbbox((0, 0), progress, font=progress_font)
    draw.text(
        (1920 - progress_box[2] / 2, progress_y),
        progress,
        fill=accent,
        font=progress_font,
    )

    highlights = event.highlights[-4:] or ["Score and status update"]
    draw.line((330, 1585, 3510, 1585), fill=muted, width=3)
    for index, highlight in enumerate(highlights):
        clean = " ".join(str(highlight).split())[:120]
        clean = clean.replace("—", "-").replace("–", "-")
        draw.text((360, 1640 + index * 72), f"* {clean}", fill=text_color, font=highlight_font)

    updated = datetime.now(timezone.utc).strftime("Updated %H:%M UTC")
    draw.text((300, 1900), updated, fill=muted, font=small_font)
    if event.start_time:
        start_box = draw.textbbox((0, 0), event.start_time, font=small_font)
        draw.text((3540 - start_box[2], 1900), event.start_time, fill=muted, font=small_font)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output_path.with_suffix(".tmp.png")
    image.save(temporary, format="PNG", optimize=True)
    os.replace(temporary, output_path)
    os.chmod(output_path, 0o600)
    return output_path


class LiveScoreService:
    """Poll providers, render on changes/schedules, and keep one TV image per tracker."""

    def __init__(self, settings_loader, *, loop_seconds: float = 5.0) -> None:
        self.settings_loader = settings_loader
        self.loop_seconds = loop_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._sportsdb_clients: dict[str, TheSportsDBClient] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="frameart-live-score", daemon=True)
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
        store = LiveScoreStore(settings.data_dir)
        results = []
        for tracker_id in store.due_tracker_ids(now):
            try:
                results.append(self.refresh_tracker(tracker_id))
            except Exception:
                logger.exception("Live score refresh failed for %s", tracker_id)
        return results

    def refresh_tracker(self, tracker_id: str, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            settings = self.settings_loader()
            store = LiveScoreStore(settings.data_dir)
            tracker = store.get_tracker(tracker_id, include_secret=True)
            if tracker is None:
                raise KeyError(tracker_id)
            if tracker["provider"] == "manual":
                event_data = tracker.get("last_event")
                if not event_data:
                    raise RuntimeError("Manual live score is waiting for its first feed update.")
                event = ScoreEvent(**event_data)
            else:
                try:
                    api_key = sportsdb_api_key(settings.data_dir, tracker.get("api_key")) or ""
                    client = self._sportsdb_client(api_key)
                    event = client.fetch(tracker["tracking_kind"], tracker["tracking_value"])
                except Exception as exc:
                    store.update_runtime(
                        tracker_id,
                        next_poll=time.time() + tracker["poll_seconds"],
                        last_status="error",
                        last_error=str(exc),
                    )
                    raise
                if event is None:
                    error = "No matching live event is currently available."
                    store.update_runtime(
                        tracker_id,
                        next_poll=time.time() + tracker["poll_seconds"],
                        last_status="waiting",
                        last_error=error,
                    )
                    return {"tracker_id": tracker_id, "status": "waiting", "detail": error}
            return self.process_event(tracker_id, event, force=force)

    def process_event(
        self,
        tracker_id: str,
        event: ScoreEvent,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        settings = self.settings_loader()
        store = LiveScoreStore(settings.data_dir)
        tracker = store.get_tracker(tracker_id, include_secret=True)
        if tracker is None:
            raise KeyError(tracker_id)
        if not event_matches(event, tracker["tracking_kind"], tracker["tracking_value"]):
            raise ValueError("Feed event does not match this tracker's configured target.")
        digest = event.digest()
        now = time.time()
        scheduled = not tracker["last_rendered"] or (
            now - float(tracker["last_rendered"]) >= tracker["refresh_seconds"]
        )
        if not force and digest == tracker["last_digest"] and not scheduled:
            store.update_runtime(
                tracker_id,
                next_poll=now + tracker["poll_seconds"],
                last_status="unchanged",
                last_error=None,
                last_event=asdict(event),
            )
            return {"tracker_id": tracker_id, "status": "unchanged"}

        previous_event = tracker.get("last_event") or {}
        highlights = list(event.highlights)
        previous_score = (previous_event.get("home_score"), previous_event.get("away_score"))
        if previous_event and previous_score != (event.home_score, event.away_score):
            highlights.append(
                f"Score update: {event.home_team} {event.home_score} - "
                f"{event.away_score} {event.away_team}"
            )
        if previous_event and previous_event.get("status") != event.status:
            highlights.append(f"Game status: {event.progress or event.status}")
        event.highlights = list(dict.fromkeys(highlights))[-8:]

        image_path = Path(settings.data_dir) / "modes" / "live-score" / tracker_id / "current.png"
        render_scoreboard(
            event,
            image_path,
            theme=tracker["theme"],
            logo_cache_dir=Path(settings.data_dir) / "cache" / "live-score-logos",
        )
        current, stale, display_results, display_errors = self._display(
            settings,
            tracker,
            image_path,
        )
        status = (
            "displayed"
            if display_results and not display_errors
            else ("partial" if display_results else "error")
        )
        error = "; ".join(display_errors) or None
        store.update_runtime(
            tracker_id,
            next_poll=now + tracker["poll_seconds"],
            last_status=status,
            last_error=error,
            last_event=asdict(event),
            last_digest=digest,
            last_rendered=now,
            current_content_ids=current,
            stale_content_ids=stale,
        )
        payload = {
            "tracker_id": tracker_id,
            "status": status,
            "event": asdict(event),
            "results": display_results,
            "errors": display_errors,
        }
        IntegrationPublisher(AutomationStore(settings.data_dir)).publish(
            f"live_score.{status}", payload
        )
        return payload

    def _sportsdb_client(self, api_key: str) -> TheSportsDBClient:
        """Reuse provider metadata caches across tracker polling cycles."""
        cache_key = hashlib.sha256(api_key.encode()).hexdigest()
        client = self._sportsdb_clients.get(cache_key)
        if client is None:
            client = TheSportsDBClient(api_key)
            self._sportsdb_clients[cache_key] = client
            while len(self._sportsdb_clients) > 4:
                self._sportsdb_clients.pop(next(iter(self._sportsdb_clients)))
        return client

    @staticmethod
    def _target_profile_ids(settings, tracker) -> tuple[list[str], str | None]:
        tv_profile_id = tracker.get("tv_profile_id")
        if tv_profile_id:
            return [tv_profile_id], None
        group = AutomationStore(settings.data_dir).get_group(tracker.get("group_id"))
        if not group:
            return [], "Configured TV group no longer exists."
        return group["tv_profile_ids"], None

    @staticmethod
    def _display(settings, tracker, image_path: Path):
        from frameart.tv.controller import delete_art, switch_art, upload_image

        profile_ids, target_error = LiveScoreService._target_profile_ids(settings, tracker)
        if target_error:
            return tracker["current_content_ids"], tracker["stale_content_ids"], [], [
                target_error
            ]
        current = dict(tracker["current_content_ids"])
        stale = {key: list(value) for key, value in tracker["stale_content_ids"].items()}
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        image_bytes = image_path.read_bytes()
        for profile_id in profile_ids:
            profile = settings.tvs.get(profile_id)
            if profile is None:
                errors.append(f"{profile_id}: TV profile is no longer configured")
                continue
            retry_ids = stale.pop(profile_id, [])
            if retry_ids:
                try:
                    if not delete_art(profile, retry_ids):
                        stale[profile_id] = retry_ids[-10:]
                except Exception:
                    stale[profile_id] = retry_ids[-10:]
            new_content_id: str | None = None
            try:
                uploaded = upload_image(profile, image_bytes, file_type="PNG", matte="none")
                if not uploaded.success or not uploaded.content_id:
                    raise RuntimeError(uploaded.error or "TV upload failed")
                new_content_id = uploaded.content_id
                if not switch_art(profile, new_content_id):
                    raise RuntimeError("TV did not switch to the new scoreboard")
                old_id = current.get(profile_id)
                current[profile_id] = new_content_id
                if old_id and old_id != new_content_id:
                    try:
                        if not delete_art(profile, [old_id]):
                            stale.setdefault(profile_id, []).append(old_id)
                    except Exception:
                        stale.setdefault(profile_id, []).append(old_id)
                results.append({"tv_profile_id": profile_id, "content_id": new_content_id})
            except Exception as exc:
                if new_content_id and current.get(profile_id) != new_content_id:
                    try:
                        if not delete_art(profile, [new_content_id]):
                            stale.setdefault(profile_id, []).append(new_content_id)
                    except Exception:
                        stale.setdefault(profile_id, []).append(new_content_id)
                errors.append(f"{profile_id}: {exc}")
        for profile_id in list(stale):
            stale[profile_id] = list(dict.fromkeys(stale[profile_id]))[-10:]
        return current, stale, results, errors

    def delete_tracker(self, tracker_id: str) -> bool:
        settings = self.settings_loader()
        store = LiveScoreStore(settings.data_dir)
        tracker = store.get_tracker(tracker_id, include_secret=True)
        if tracker is None:
            return False
        from frameart.tv.controller import delete_art

        profile_ids, _target_error = self._target_profile_ids(settings, tracker)
        for profile_id in profile_ids:
            profile = settings.tvs.get(profile_id)
            ids = [tracker["current_content_ids"].get(profile_id)]
            ids.extend(tracker["stale_content_ids"].get(profile_id, []))
            content_ids = list(dict.fromkeys(content_id for content_id in ids if content_id))
            if profile and content_ids:
                try:
                    delete_art(profile, content_ids)
                except Exception:
                    logger.warning("Could not clean up live-score TV art for %s", profile_id)
        deleted = store.delete_tracker(tracker_id)
        mode_dir = Path(settings.data_dir) / "modes" / "live-score" / tracker_id
        if mode_dir.is_dir():
            shutil.rmtree(mode_dir)
        return deleted
