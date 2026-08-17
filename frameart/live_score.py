"""Live score tracking, deterministic scoreboard rendering, and bounded TV storage."""

from __future__ import annotations

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
from pathlib import Path
from typing import Any

import httpx2 as httpx
from PIL import Image, ImageDraw, ImageFont

from frameart.automation import AutomationStore, IntegrationPublisher

logger = logging.getLogger(__name__)

_SPORTSDB_BASE_URL = "https://www.thesportsdb.com/api/v2/json"


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
        group_id: str,
        poll_seconds: int,
        refresh_seconds: int,
        theme: str,
        enabled: bool,
    ) -> dict[str, Any]:
        now = time.time()
        values = {
            "id": uuid.uuid4().hex,
            "name": name.strip(),
            "provider": provider,
            "api_key": api_key,
            "tracking_kind": tracking_kind,
            "tracking_value": tracking_value.strip(),
            "group_id": group_id,
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
                    group_id, poll_seconds, refresh_seconds, theme, enabled,
                    next_poll, current_content_ids, stale_content_ids, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?)
                """,
                (
                    values["id"], values["name"], provider, api_key, tracking_kind,
                    values["tracking_value"], group_id, poll_seconds, refresh_seconds,
                    theme, int(enabled), now, now,
                ),
            )
        return self.get_tracker(values["id"]) or values

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
        rows = payload if isinstance(payload, list) else (
            payload.get("livescores") or payload.get("events") or payload.get("data") or []
        )
        events = [self._normalize(row) for row in rows if isinstance(row, dict)]
        return next(
            (event for event in events if event_matches(event, tracking_kind, tracking_value)),
            None,
        )

    @staticmethod
    def _normalize(row: dict[str, Any]) -> ScoreEvent:
        def text(*keys: str, default: str = "") -> str:
            for key in keys:
                value = row.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
            return default

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


def render_scoreboard(event: ScoreEvent, output_path: Path, *, theme: str = "dark") -> Path:
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

    league_font = _font(66, bold=True)
    status_font = _font(58, bold=True)
    team_font_home = _fit_text(draw, event.home_team, 1180, 128)
    team_font_away = _fit_text(draw, event.away_team, 1180, 128)
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

    draw.text((330, 650), event.home_team, fill=text_color, font=team_font_home)
    away_box = draw.textbbox((0, 0), event.away_team, font=team_font_away)
    draw.text((3510 - away_box[2], 650), event.away_team, fill=text_color, font=team_font_away)

    home_score_box = draw.textbbox((0, 0), event.home_score, font=score_font)
    away_score_box = draw.textbbox((0, 0), event.away_score, font=score_font)
    draw.text(
        (980 - home_score_box[2] / 2, 910),
        event.home_score,
        fill=text_color,
        font=score_font,
    )
    draw.text(
        (2860 - away_score_box[2] / 2, 910),
        event.away_score,
        fill=text_color,
        font=score_font,
    )
    draw.text((1835, 955), "-", fill=accent, font=score_font)

    progress = event.status if event.status != status_label else "LIVE"
    progress_box = draw.textbbox((0, 0), progress, font=progress_font)
    draw.text((1920 - progress_box[2] / 2, 1325), progress, fill=accent, font=progress_font)

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
                    event = TheSportsDBClient(tracker.get("api_key") or "").fetch(
                        tracker["tracking_kind"], tracker["tracking_value"]
                    )
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
        render_scoreboard(event, image_path, theme=tracker["theme"])
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

    @staticmethod
    def _display(settings, tracker, image_path: Path):
        from frameart.tv.controller import delete_art, switch_art, upload_image

        group = AutomationStore(settings.data_dir).get_group(tracker["group_id"])
        if not group:
            return tracker["current_content_ids"], tracker["stale_content_ids"], [], [
                "Configured TV group no longer exists."
            ]
        current = dict(tracker["current_content_ids"])
        stale = {key: list(value) for key, value in tracker["stale_content_ids"].items()}
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        image_bytes = image_path.read_bytes()
        for profile_id in group["tv_profile_ids"]:
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

        group = AutomationStore(settings.data_dir).get_group(tracker["group_id"])
        profile_ids = group["tv_profile_ids"] if group else []
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
