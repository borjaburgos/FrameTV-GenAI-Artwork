"""Durable, calendar-based daily artwork delivery.

This module never talks to a TV or reads NAS files.  The supplied delivery
callback owns those boundaries and must honor ``skip_tvs`` before any mutation.
A committed dispatch claim is never replayed after an unknown outcome.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_RESULT_STATUSES = {"displayed", "deferred", "error", "uncertain"}
_DEFAULTS = {
    "enabled": False,
    "timezone": "America/New_York",
    "change_time": "00:00",
    "target_type": "tv",
    "target_id": None,
    "hold_until": None,
}
# A local day can contain 25 elapsed hours during the autumn DST transition.
# Never exhaust hourly retries before that day ends.
_MAX_ERRORS = 48
_DEFAULT_RETRY_SECONDS = 3600


def _now(value: datetime | float | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Daily Art timestamps must include a timezone.")
    return value.astimezone(timezone.utc)


def _zone(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("Choose a valid IANA timezone.")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Choose a valid IANA timezone.") from exc
    return value


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Invalid {label}.")
    return value


def _iso_instant(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("hold_until must be a timezone-aware ISO timestamp.")
    try:
        return _now(datetime.fromisoformat(value.replace("Z", "+00:00"))).isoformat()
    except (ValueError, TypeError) as exc:
        raise ValueError("hold_until must be a timezone-aware ISO timestamp.") from exc


class DailyArtStore:
    """Small SQLite state machine, separate from source artwork and TV storage."""

    def __init__(self, data_dir: Path) -> None:
        Path(data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = Path(data_dir) / "daily-art.sqlite3"
        if self.path.is_symlink():
            raise ValueError("The Daily Art database must not be a symbolic link.")
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            os.chmod(self.path, 0o600)
        else:
            os.close(descriptor)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_art_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_art_calendar (
                    day TEXT PRIMARY KEY, artwork_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_art_runs (
                    day_key TEXT PRIMARY KEY, artwork_id TEXT NOT NULL,
                    target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                    claim_id TEXT, claimed_at REAL, status TEXT NOT NULL,
                    next_attempt REAL, attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS daily_art_results (
                    day_key TEXT NOT NULL, tv TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL, updated_at REAL NOT NULL,
                    errors INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day_key, tv)
                );
                CREATE TABLE IF NOT EXISTS daily_art_holds (
                    tv TEXT NOT NULL, day_key TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(tv, day_key)
                );
                CREATE TABLE IF NOT EXISTS daily_art_album_priority (
                    tv TEXT PRIMARY KEY, album_id TEXT NOT NULL,
                    candidate TEXT NOT NULL, pending INTEGER NOT NULL,
                    upload_attempted INTEGER NOT NULL DEFAULT 0,
                    detected_at REAL NOT NULL, until REAL
                );
                """
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _state(connection: sqlite3.Connection, key: str, default=None):
        row = connection.execute(
            "SELECT value FROM daily_art_state WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    @staticmethod
    def _put(connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO daily_art_state(key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )

    def config(self) -> dict[str, Any]:
        with self._connect() as connection:
            return {**_DEFAULTS, **self._state(connection, "config", {})}

    def configure(self, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(config, dict) or set(config) - {*_DEFAULTS, "clear_holds"}:
            raise ValueError("Daily Art settings contain unsupported fields.")
        for flag in ("enabled", "clear_holds"):
            if flag in config and not isinstance(config[flag], bool):
                raise ValueError(f"{flag} must be true or false.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = {**_DEFAULTS, **self._state(connection, "config", {})}
            current.update({key: value for key, value in config.items() if key in _DEFAULTS})
            current["timezone"] = _zone(current["timezone"])
            if not isinstance(current["change_time"], str) or not _TIME.fullmatch(
                current["change_time"]
            ):
                raise ValueError("change_time must use 24-hour HH:MM format.")
            if not isinstance(current["target_type"], str) or current["target_type"] not in {
                "tv",
                "group",
            }:
                raise ValueError("target_type must be tv or group.")
            if current["target_id"] is not None and current["target_id"] != "":
                current["target_id"] = _identity(current["target_id"], "TV or group ID")
            else:
                current["target_id"] = None
            if current["enabled"] and not current["target_id"]:
                raise ValueError("Choose a TV or TV group before enabling Daily Art.")
            current["hold_until"] = _iso_instant(current["hold_until"])
            self._put(connection, "config", current)
            if config.get("clear_holds"):
                held_days = [
                    row[0]
                    for row in connection.execute("SELECT DISTINCT day_key FROM daily_art_holds")
                ]
                connection.execute("DELETE FROM daily_art_holds")
                connection.executemany(
                    "UPDATE daily_art_runs SET status='partial',next_attempt=0 "
                    "WHERE day_key=? AND claim_id IS NULL AND status!='uncertain_dispatch'",
                    [(day,) for day in held_days],
                )
        return current

    def set_calendar(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - {"year", "timezone", "entries"}:
            raise ValueError("Use a calendar containing year, timezone and entries.")
        year = payload.get("year")
        if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 9998:
            raise ValueError("Calendar year is invalid.")
        calendar_zone = _zone(payload.get("timezone", "America/New_York"))
        entries = payload.get("entries")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 366:
            raise ValueError("A calendar must contain between 1 and 366 days.")
        rows: list[tuple[str, str]] = []
        dates, identities = set(), set()
        allowed = {"date", "artwork_id", "month", "day_of_year", "season", "occasion"}
        for item in entries:
            if not isinstance(item, dict) or set(item) - allowed:
                raise ValueError("Calendar entries contain unsupported fields.")
            try:
                day = date.fromisoformat(item.get("date", ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("Calendar dates must use YYYY-MM-DD format.") from exc
            if day.isoformat() != item.get("date") or day.year != year:
                raise ValueError("Every calendar date must belong to its declared year.")
            identity = _identity(item.get("artwork_id"), "calendar artwork ID")
            if day in dates or identity in identities:
                raise ValueError("Calendar dates and artwork IDs must be unique.")
            if "month" in item and item["month"] != day.month:
                raise ValueError("Calendar month does not match its date.")
            if "day_of_year" in item and item["day_of_year"] != day.timetuple().tm_yday:
                raise ValueError("Calendar day_of_year does not match its date.")
            dates.add(day)
            identities.add(identity)
            rows.append((day.isoformat(), identity))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempted = {
                row["day_key"]: row["artwork_id"]
                for row in connection.execute("SELECT day_key, artwork_id FROM daily_art_runs")
            }
            if any(day in attempted and attempted[day] != identity for day, identity in rows):
                raise ValueError("A calendar cannot change artwork for an already attempted day.")
            # Keep adjacent years together so a short commissioning calendar
            # can roll straight into the full next-year calendar on January 1.
            connection.execute(
                "DELETE FROM daily_art_calendar WHERE day >= ? AND day <= ?",
                (f"{year:04d}-01-01", f"{year:04d}-12-31"),
            )
            connection.executemany(
                "INSERT INTO daily_art_calendar(day, artwork_id) VALUES (?, ?)", rows
            )
            self._put(connection, "calendar", {"year": year, "timezone": calendar_zone})
            calendars = self._state(connection, "calendars", {})
            calendars[str(year)] = {"year": year, "timezone": calendar_zone}
            self._put(connection, "calendars", calendars)
        return {"calendar_year": year, "calendar_count": len(rows), "timezone": calendar_zone}

    def note_manual_choice(self, tv_id: str, now: datetime | float | None = None) -> None:
        tv_id = _identity(tv_id, "TV ID")
        instant = _now(now)
        day_key = instant.astimezone(ZoneInfo(self.config()["timezone"])).date().isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO daily_art_holds(tv, day_key, created_at) VALUES (?, ?, ?)",
                (tv_id, day_key, instant.timestamp()),
            )

    @staticmethod
    def _album_holds(connection, instant: datetime, timezone_name: str) -> list[dict]:
        day_key = instant.astimezone(ZoneInfo(timezone_name)).date().isoformat()
        return [
            {
                "tv": row["tv"],
                "date": day_key,
                "reason": "family-album",
                "album_id": row["album_id"],
                "pending": bool(row["pending"]),
                "until": datetime.fromtimestamp(row["until"], timezone.utc).isoformat()
                if row["until"] is not None
                else None,
            }
            for row in connection.execute(
                "SELECT * FROM daily_art_album_priority WHERE pending=1 OR until>? ORDER BY tv",
                (instant.timestamp(),),
            )
        ]

    def begin_album_priority(
        self,
        album_id: str,
        tv_ids: list[str],
        candidate: str,
        now: datetime | float | None = None,
    ) -> None:
        """Commit the newest photo's priority before waiting for a TV lock.

        A repeated observation never extends a confirmed hold or resets an
        uncertain upload. Only a genuinely new candidate supersedes it.
        """
        _identity(album_id, "album ID")
        _identity(candidate, "album photo version")
        instant = _now(now)
        for tv in tv_ids:
            _identity(tv, "TV ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for tv in dict.fromkeys(tv_ids):
                connection.execute(
                    "INSERT INTO daily_art_album_priority "
                    "(tv,album_id,candidate,pending,upload_attempted,detected_at,until) "
                    "VALUES(?,?,?,1,0,?,NULL) ON CONFLICT(tv) DO UPDATE SET "
                    "album_id=excluded.album_id,candidate=excluded.candidate,pending=1,"
                    "upload_attempted=0,detected_at=excluded.detected_at,until=NULL "
                    "WHERE daily_art_album_priority.album_id!=excluded.album_id "
                    "OR daily_art_album_priority.candidate!=excluded.candidate",
                    (tv, album_id, candidate, instant.timestamp()),
                )

    def album_priority_pending(self, tv_id: str, album_id: str, candidate: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM daily_art_album_priority "
                    "WHERE tv=? AND album_id=? AND candidate=? AND pending=1",
                    (tv_id, album_id, candidate),
                ).fetchone()
                is not None
            )

    def claim_album_upload(self, tv_id: str, album_id: str, candidate: str) -> bool:
        """An unknown post-send upload is never blindly replayed after restart."""
        with self._connect() as connection:
            return (
                connection.execute(
                    "UPDATE daily_art_album_priority SET upload_attempted=1 "
                    "WHERE tv=? AND album_id=? AND candidate=? "
                    "AND pending=1 AND upload_attempted=0",
                    (tv_id, album_id, candidate),
                ).rowcount
                == 1
            )

    def defer_album_upload(self, tv_id: str, album_id: str, candidate: str) -> None:
        """Only a positive pre-mutation Art Mode refusal is safe to retry."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE daily_art_album_priority SET upload_attempted=0 "
                "WHERE tv=? AND album_id=? AND candidate=? AND pending=1",
                (tv_id, album_id, candidate),
            )

    def confirm_album_priority(
        self,
        tv_id: str,
        album_id: str,
        candidate: str,
        now: datetime | float | None = None,
    ) -> bool:
        """After confirmed selection, keep priority only until local midnight."""
        instant = _now(now)
        zone = ZoneInfo(self.config()["timezone"])
        tomorrow = instant.astimezone(zone).date() + timedelta(days=1)
        midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=zone)
        with self._connect() as connection:
            return (
                connection.execute(
                    "UPDATE daily_art_album_priority SET pending=0,until=? "
                    "WHERE tv=? AND album_id=? AND candidate=? AND pending=1",
                    (midnight.timestamp(), tv_id, album_id, candidate),
                ).rowcount
                == 1
            )

    def reconcile_album_priority(
        self,
        album_id: str,
        tv_ids: list[str],
        candidate: str | None,
    ) -> None:
        """Release obsolete pending photos, never reannounce a deletion fallback."""
        today = _now().astimezone(ZoneInfo(self.config()["timezone"])).date().isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT tv,candidate FROM daily_art_album_priority WHERE album_id=? AND pending=1",
                (album_id,),
            ).fetchall()
            for row in rows:
                if row["tv"] not in tv_ids or row["candidate"] != candidate:
                    connection.execute(
                        "DELETE FROM daily_art_album_priority "
                        "WHERE tv=? AND album_id=? AND pending=1",
                        (row["tv"], album_id),
                    )
                    connection.execute(
                        "UPDATE daily_art_runs SET status='partial',next_attempt=0 "
                        "WHERE day_key=? AND status='held' AND claim_id IS NULL",
                        (today,),
                    )

    def snapshot(self, now: datetime | float | None = None) -> dict[str, Any]:
        config = self.config()
        instant = _now(now)
        local = instant.astimezone(ZoneInfo(config["timezone"]))
        today = local.date()
        with self._connect() as connection:
            calendar = self._state(connection, "calendar", {})
            calendars = self._state(connection, "calendars", {})
            years = [
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT substr(day,1,4) FROM daily_art_calendar ORDER BY day"
                )
            ]
            if today.year in years:
                calendar = calendars.get(str(today.year), calendar)
            total_count = connection.execute("SELECT COUNT(*) FROM daily_art_calendar").fetchone()[
                0
            ]
            count = connection.execute(
                "SELECT COUNT(*) FROM daily_art_calendar WHERE substr(day,1,4)=?",
                (str(calendar.get("year", "")),),
            ).fetchone()[0]
            results = [
                {key: row[key] for key in ("tv", "status", "detail")}
                for row in connection.execute(
                    "SELECT * FROM daily_art_results WHERE day_key = ? ORDER BY tv",
                    (today.isoformat(),),
                )
            ]
            holds = [
                {"tv": row["tv"], "date": row["day_key"]}
                for row in connection.execute(
                    "SELECT tv, day_key FROM daily_art_holds WHERE day_key = ? ORDER BY tv",
                    (today.isoformat(),),
                )
            ]
            holds.extend(self._album_holds(connection, instant, config["timezone"]))
            days = []
            for day in (today, today + timedelta(days=1)):
                key = day.isoformat()
                entry = connection.execute(
                    "SELECT artwork_id FROM daily_art_calendar WHERE day = ?", (key,)
                ).fetchone()
                run = connection.execute(
                    "SELECT status,next_attempt FROM daily_art_runs WHERE day_key = ?", (key,)
                ).fetchone()
                status = run["status"] if run else ("pending" if entry else "not_covered")
                if entry and day == today:
                    if not config["enabled"]:
                        status = "disabled"
                    elif (
                        config["hold_until"]
                        and instant < datetime.fromisoformat(config["hold_until"])
                    ) or (
                        config["target_type"] == "tv"
                        and any(hold["tv"] == config["target_id"] for hold in holds)
                    ):
                        status = "held"
                    elif not run and local.strftime("%H:%M") < config["change_time"]:
                        status = "scheduled"
                days.append(
                    {
                        "date": key,
                        "artwork_id": entry["artwork_id"] if entry else None,
                        "status": status,
                        "next_retry_at": (
                            datetime.fromtimestamp(run["next_attempt"], timezone.utc).isoformat()
                            if run and run["next_attempt"] is not None
                            else None
                        ),
                    }
                )
        return {
            **config,
            "calendar_year": calendar.get("year"),
            "calendar_timezone": calendar.get("timezone"),
            "calendar_count": count,
            "calendar_years": years,
            "total_calendar_count": total_count,
            "calendar_covered": days[0]["artwork_id"] is not None,
            "today": days[0],
            "tomorrow": days[1],
            "results": results,
            "holds": holds,
            "retry_interval_seconds": _DEFAULT_RETRY_SECONDS,
            "next_retry_at": days[0]["next_retry_at"],
            "pending": days[0]["status"] in {"pending", "scheduled", "deferred", "partial"}
            or (days[0]["status"] == "uncertain" and days[0]["next_retry_at"] is not None),
        }

    def _claim(
        self, instant: datetime, retry_seconds: float, *, reconcile_uncertain: bool = False
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            config = {**_DEFAULTS, **self._state(connection, "config", {})}
            local = instant.astimezone(ZoneInfo(config["timezone"]))
            day_key = local.date().isoformat()
            if not config["enabled"] or not config["target_id"]:
                return None
            if local.strftime("%H:%M") < config["change_time"]:
                return None
            if config["hold_until"] and instant < datetime.fromisoformat(config["hold_until"]):
                return None
            high_water = self._state(connection, "last_dispatch_day")
            if high_water and day_key < high_water:
                return None
            entry = connection.execute(
                "SELECT artwork_id FROM daily_art_calendar WHERE day = ?", (day_key,)
            ).fetchone()
            if entry is None:
                return None
            run = connection.execute(
                "SELECT * FROM daily_art_runs WHERE day_key = ?", (day_key,)
            ).fetchone()
            if run and (run["claim_id"] or run["status"] == "uncertain_dispatch"):
                return None
            same_target = run and (run["target_type"], run["target_id"]) == (
                config["target_type"],
                config["target_id"],
            )
            if (
                run
                and run["status"] == "uncertain"
                and run["next_attempt"] is None
                and reconcile_uncertain
            ):
                due = (run["claimed_at"] or instant.timestamp()) + retry_seconds
                connection.execute(
                    "UPDATE daily_art_runs SET next_attempt=? WHERE day_key=?", (due, day_key)
                )
                run = connection.execute(
                    "SELECT * FROM daily_art_runs WHERE day_key=?", (day_key,)
                ).fetchone()
            if (
                same_target
                and run["status"] in {"displayed", "held", "uncertain", "error"}
                and run["next_attempt"] is None
            ):
                return None
            if (
                run
                and run["next_attempt"] is not None
                and instant.timestamp() < run["next_attempt"]
            ):
                return None
            prior = list(
                connection.execute(
                    "SELECT tv, status, errors FROM daily_art_results WHERE day_key = ?", (day_key,)
                )
            )
            held = {
                row["tv"]
                for row in connection.execute(
                    "SELECT tv FROM daily_art_holds WHERE day_key = ?", (day_key,)
                )
            }
            held.update(
                item["tv"] for item in self._album_holds(connection, instant, config["timezone"])
            )
            skip = held | {
                row["tv"]
                for row in prior
                if row["status"] in {"displayed", "uncertain"} or row["errors"] >= _MAX_ERRORS
            }
            if config["target_type"] == "tv" and config["target_id"] in held:
                return None
            claim_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO daily_art_runs
                (day_key, artwork_id, target_type, target_id, claim_id, claimed_at,
                 status, next_attempt, attempts) VALUES (?, ?, ?, ?, ?, ?, 'dispatching', ?, 1)
                ON CONFLICT(day_key) DO UPDATE SET claim_id=excluded.claim_id,
                claimed_at=excluded.claimed_at, status='dispatching',
                target_type=excluded.target_type, target_id=excluded.target_id,
                next_attempt=excluded.next_attempt, attempts=daily_art_runs.attempts+1""",
                (
                    day_key,
                    entry["artwork_id"],
                    config["target_type"],
                    config["target_id"],
                    claim_id,
                    instant.timestamp(),
                    instant.timestamp() + retry_seconds,
                ),
            )
            self._put(connection, "last_dispatch_day", day_key)
            return {
                "day_key": day_key,
                "artwork_id": entry["artwork_id"],
                "target_type": config["target_type"],
                "target_id": config["target_id"],
                "claim_id": claim_id,
                "skip_tvs": skip,
                "uncertain_tvs": [
                    row["tv"]
                    for row in prior
                    if row["status"] == "uncertain" and row["tv"] not in held
                ],
            }

    def _finish(
        self,
        claim: dict,
        outcome: Any,
        instant: datetime,
        retry_seconds: float,
        *,
        reconcile_uncertain: bool = False,
    ) -> None:
        results = outcome.get("results") if isinstance(outcome, dict) else None
        valid = isinstance(results, list) and len(results) <= 100
        seen = set()
        if valid:
            for result in results:
                if not isinstance(result, dict) or result.get("status") not in _RESULT_STATUSES:
                    valid = False
                    break
                tv = result.get("tv")
                if not isinstance(tv, str) or not _ID.fullmatch(tv) or tv in seen:
                    valid = False
                    break
                if claim["target_type"] == "tv" and tv != claim["target_id"]:
                    valid = False
                    break
                if tv in claim["skip_tvs"] and result["status"] != "deferred":
                    valid = False
                    break
                if not isinstance(result.get("detail", ""), str):
                    valid = False
                    break
                seen.add(tv)
        if valid and not results and not claim["skip_tvs"]:
            valid = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT claim_id FROM daily_art_runs WHERE day_key = ?", (claim["day_key"],)
            ).fetchone()
            if run is None or run["claim_id"] != claim["claim_id"]:
                return
            if not valid:
                connection.execute(
                    "UPDATE daily_art_runs SET claim_id=NULL, status='uncertain_dispatch', "
                    "next_attempt=NULL WHERE day_key=?",
                    (claim["day_key"],),
                )
                return
            for result in results:
                if result["tv"] in claim["skip_tvs"]:
                    continue
                previous = connection.execute(
                    "SELECT errors FROM daily_art_results WHERE day_key=? AND tv=?",
                    (claim["day_key"], result["tv"]),
                ).fetchone()
                errors = (previous["errors"] if previous else 0) + int(result["status"] == "error")
                connection.execute(
                    "INSERT OR REPLACE INTO daily_art_results "
                    "(day_key,tv,status,detail,updated_at,errors) VALUES (?,?,?,?,?,?)",
                    (
                        claim["day_key"],
                        result["tv"],
                        result["status"],
                        result.get("detail", "")[:500],
                        instant.timestamp(),
                        errors,
                    ),
                )
            all_results = list(
                connection.execute(
                    "SELECT status, errors FROM daily_art_results WHERE day_key=?",
                    (claim["day_key"],),
                )
            )
            statuses = {row["status"] for row in all_results}
            retryable = [
                row
                for row in all_results
                if row["status"] == "deferred"
                or (row["status"] == "error" and row["errors"] < _MAX_ERRORS)
            ]
            status = "held" if not all_results else "displayed"
            if "uncertain" in statuses:
                status = "uncertain"
            elif retryable:
                status = "partial" if "displayed" in statuses else "deferred"
            elif "error" in statuses:
                status = "error"
            retry_due = bool(retryable) or (reconcile_uncertain and "uncertain" in statuses)
            connection.execute(
                "UPDATE daily_art_runs SET claim_id=NULL,status=?,next_attempt=? WHERE day_key=?",
                (
                    status,
                    instant.timestamp() + retry_seconds if retry_due else None,
                    claim["day_key"],
                ),
            )

    def _apply_reconciliation(self, claim: dict, tv_id: str, outcome: str) -> bool:
        """Apply read-only proof only while this worker still owns its dispatch claim."""
        if not isinstance(outcome, str) or outcome not in {"displayed", "retryable"}:
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT claim_id FROM daily_art_runs WHERE day_key=?", (claim["day_key"],)
            ).fetchone()
            if run is None or run["claim_id"] != claim["claim_id"]:
                return False
            cursor = connection.execute(
                "UPDATE daily_art_results SET status=?,detail='Read-only inventory reconciled', "
                "errors=0 WHERE day_key=? AND tv=? AND status='uncertain'",
                ("displayed" if outcome == "displayed" else "deferred", claim["day_key"], tv_id),
            )
            return cursor.rowcount == 1

    def resolve_uncertain(self, day_key: str, tv_id: str, *, displayed: bool) -> None:
        """Only call after an independent, explicit TV-inventory reconciliation."""
        _identity(tv_id, "TV ID")
        if not isinstance(displayed, bool):
            raise ValueError("A definite reconciliation result is required.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM daily_art_results WHERE day_key=? AND tv=?", (day_key, tv_id)
            ).fetchone()
            if row is None or row["status"] != "uncertain":
                raise ValueError("This TV has no individually uncertain delivery to reconcile.")
            status = "displayed" if displayed else "deferred"
            connection.execute(
                "UPDATE daily_art_results SET status=?,detail='Inventory reconciled',errors=0 "
                "WHERE day_key=? AND tv=?",
                (status, day_key, tv_id),
            )
            connection.execute(
                "UPDATE daily_art_runs SET status='partial',next_attempt=0 WHERE day_key=? "
                "AND claim_id IS NULL AND status!='uncertain_dispatch'",
                (day_key,),
            )


class DailyArtWorker:
    """Dispatch today's choice only; never catch up by showing missed days."""

    def __init__(
        self,
        settings_loader,
        deliver,
        *,
        interval_seconds: float = 30,
        retry_seconds: float = _DEFAULT_RETRY_SECONDS,
        reconcile=None,
    ) -> None:
        self.settings_loader = settings_loader
        self.deliver = deliver
        self.reconcile = reconcile
        self.interval_seconds = max(0.1, interval_seconds)
        self.retry_seconds = max(1, retry_seconds)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="daily-art", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Never log callback exception strings: upstream URLs may be sensitive.
            with suppress(Exception):
                self.tick()
            self._stop.wait(self.interval_seconds)

    def tick(self, now: datetime | float | None = None) -> dict[str, Any]:
        instant = _now(now)
        store = DailyArtStore(self.settings_loader().data_dir)
        if not self._lock.acquire(blocking=False):
            return store.snapshot(instant)
        try:
            claim = store._claim(
                instant, self.retry_seconds, reconcile_uncertain=self.reconcile is not None
            )
            if claim is None:
                return store.snapshot(instant)
            if self.reconcile is not None:
                for tv in claim["uncertain_tvs"]:
                    try:
                        result = self.reconcile(
                            claim["artwork_id"],
                            claim["target_type"],
                            claim["target_id"],
                            claim["day_key"],
                            tv,
                        )
                    except Exception:
                        result = "uncertain"
                    if store._apply_reconciliation(claim, tv, result):
                        if result == "retryable":
                            claim["skip_tvs"].discard(tv)
                        else:
                            claim["skip_tvs"].add(tv)
            try:
                if claim["target_type"] == "tv" and claim["target_id"] in claim["skip_tvs"]:
                    outcome = {"results": []}
                else:
                    outcome = self.deliver(
                        claim["artwork_id"],
                        claim["target_type"],
                        claim["target_id"],
                        claim["day_key"],
                        skip_tvs=set(claim["skip_tvs"]),
                    )
            except Exception:
                outcome = None  # Unknown side effects: persist a non-replayable outcome.
            store._finish(
                claim,
                outcome,
                instant,
                self.retry_seconds,
                reconcile_uncertain=self.reconcile is not None,
            )
            return store.snapshot(instant)
        finally:
            self._lock.release()

    def run(self, now: datetime | float | None = None) -> dict[str, Any]:
        """Run one ordinary due check, never override holds or the local clock."""
        return self.tick(now)
