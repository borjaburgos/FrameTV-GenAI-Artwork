"""Cached artwork browsing and explicit, queued administrator/control actions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from frameart.art_catalog import (
    ArtCatalogStore,
    integration_configured,
)
from frameart.art_delivery import CatalogDeliveryService, CatalogDeliveryStore, resolve_targets
from frameart.catalog_sync import start_sync, sync_progress
from frameart.daily_art import DailyArtStore

_ARTWORK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_MAX_CALENDAR_BYTES = 512 * 1024


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Choose a valid IANA timezone.") from None
    return value


class CatalogSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_type: str = Field(pattern=r"^(tv|group)$")
    target_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class DailyArtConfigRequest(BaseModel):
    """Strict partial update; omitted fields never overwrite saved settings."""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    timezone: str = Field("America/New_York", max_length=100)
    change_time: str = Field("09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    target_type: Literal["tv", "group"] = "tv"
    target_id: str | None = Field(None, pattern=_ARTWORK_ID.pattern)
    hold_until: str | None = Field(None, max_length=100)
    clear_holds: bool = False

    _valid_timezone = field_validator("timezone")(_timezone)

    @field_validator("hold_until")
    @classmethod
    def valid_hold(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise ValueError("hold_until must be a timezone-aware ISO timestamp.") from None
        return value


class CalendarEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    artwork_id: str = Field(pattern=_ARTWORK_ID.pattern)
    month: int = Field(1, ge=1, le=12)
    day_of_year: int = Field(1, ge=1, le=366)
    season: str = Field("", max_length=100)
    occasion: str = Field("", max_length=1000)

    @model_validator(mode="after")
    def matching_date(self):
        try:
            day = date.fromisoformat(self.date)
        except ValueError:
            raise ValueError("Calendar dates must use valid YYYY-MM-DD dates.") from None
        if "month" in self.model_fields_set and self.month != day.month:
            raise ValueError("Calendar month does not match its date.")
        if "day_of_year" in self.model_fields_set and self.day_of_year != day.timetuple().tm_yday:
            raise ValueError("Calendar day_of_year does not match its date.")
        return self


class CalendarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    year: int = Field(ge=1900, le=9998)
    timezone: str = Field("America/New_York", max_length=100)
    entries: list[CalendarEntry] = Field(min_length=1, max_length=366)

    _valid_timezone = field_validator("timezone")(_timezone)

    @model_validator(mode="after")
    def unique_entries(self):
        if any(date.fromisoformat(entry.date).year != self.year for entry in self.entries):
            raise ValueError("Every calendar date must belong to its declared year.")
        if len({entry.date for entry in self.entries}) != len(self.entries) or len(
            {entry.artwork_id for entry in self.entries}
        ) != len(self.entries):
            raise ValueError("Calendar dates and artwork IDs must be unique.")
        return self


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate calendar object keys are not allowed.")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Calendar JSON must not contain non-finite numbers.")


def create_art_catalog_router(
    settings_loader: Callable,
    delivery: CatalogDeliveryService | None = None,
) -> APIRouter:
    """Mount behind FrameArt's existing authentication and admin middleware."""
    router = APIRouter()
    delivery = delivery or CatalogDeliveryService(settings_loader)

    @router.get("/art-catalog")
    def catalog(response: Response):
        response.headers["Cache-Control"] = "no-store"
        settings = settings_loader()
        result = ArtCatalogStore(settings.data_dir).snapshot(settings)
        result["sync"] = sync_progress(settings)
        return result

    @router.post("/art-catalog/sync", status_code=202)
    def refresh(response: Response):
        response.headers["Cache-Control"] = "no-store"
        settings = settings_loader()
        if not integration_configured(settings):
            raise HTTPException(
                503, "Configure the ResourceSpace server, reader key and collection first."
            )
        return start_sync(settings)

    @router.post("/art-catalog/{artwork_id}/send", status_code=202)
    def send(artwork_id: str, body: CatalogSendRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return delivery.enqueue(artwork_id, body.target_type, body.target_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/art-catalog/deliveries/{job_id}")
    def delivery_status(job_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise HTTPException(400, "Invalid delivery request ID.")
        job = CatalogDeliveryStore(settings_loader().data_dir).get(job_id)
        if job is None:
            raise HTTPException(404, "Delivery request was not found.")
        return job

    @router.get("/modes/daily-art")
    def daily_art(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return DailyArtStore(settings_loader().data_dir).snapshot()

    @router.put("/modes/daily-art")
    def configure_daily_art(config: dict, response: Response):
        response.headers["Cache-Control"] = "no-store"
        settings = settings_loader()
        store = DailyArtStore(settings.data_dir)
        try:
            update = DailyArtConfigRequest.model_validate(config).model_dump(exclude_unset=True)
            candidate = {**store.config(), **update}
            if candidate["enabled"] and not candidate["target_id"]:
                raise ValueError("Choose a TV or TV group before enabling Daily Art.")
            if candidate["target_id"] and (
                candidate["enabled"] or {"target_id", "target_type"}.intersection(update)
            ):
                targets = set(
                    resolve_targets(settings, candidate["target_type"], candidate["target_id"])
                )
                if candidate["enabled"]:
                    from frameart.live_score import LiveScoreStore

                    # Album new-photo events deliberately take priority until
                    # midnight. Sports remains mutually exclusive; never disable it.
                    modes = LiveScoreStore(settings.data_dir).list_trackers()
                    for mode in modes:
                        if not mode.get("enabled"):
                            continue
                        kind = mode.get("target_type") or (
                            "group" if mode.get("group_id") else "tv"
                        )
                        identity = (
                            mode.get("target_id")
                            or mode.get("tv_profile_id")
                            or mode.get("group_id")
                        )
                        try:
                            overlaps = identity and targets.intersection(
                                resolve_targets(settings, kind, identity)
                            )
                        except ValueError:
                            raise HTTPException(
                                409,
                                "Resolve the enabled sports feed's missing target "
                                "before enabling Daily Art.",
                            ) from None
                        if overlaps:
                            raise HTTPException(
                                409,
                                "Pause the overlapping sports feed before enabling Daily Art.",
                            )
            if candidate.get("enabled") and not store.snapshot()["calendar_count"]:
                raise HTTPException(409, "Import the artwork calendar before enabling Daily Art.")
            store.configure(update)
        except ValidationError:
            raise HTTPException(
                400, "Daily Art settings contain invalid or unsupported values."
            ) from None
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from None
        return store.snapshot()

    @router.post("/modes/daily-art/calendar")
    async def import_calendar(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        # Bound streamed input before parsing, not merely its declared length.
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_CALENDAR_BYTES:
                raise HTTPException(413, "Calendar file is too large.")
            raw.extend(chunk)

        settings = settings_loader()
        try:
            payload = json.loads(
                raw, object_pairs_hook=_unique_json_object, parse_constant=_invalid_constant
            )
            calendar = CalendarRequest.model_validate(payload)
            catalog = ArtCatalogStore(settings.data_dir)
            known_ids = {item["id"] for item in catalog.snapshot(settings)["items"]}
            if any(entry.artwork_id not in known_ids for entry in calendar.entries):
                raise HTTPException(
                    409, "Sync all calendar artwork into the catalog before importing it."
                )
            DailyArtStore(settings.data_dir).set_calendar(calendar.model_dump(exclude_unset=True))
        except ValidationError:
            raise HTTPException(
                400, "The calendar file contains invalid or unsupported values."
            ) from None
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "The calendar file is invalid: " + str(exc)) from None
        return DailyArtStore(settings.data_dir).snapshot()

    @router.get("/art-catalog/{artwork_id}/thumbnail")
    def thumbnail(artwork_id: str, request: Request):
        if not _ARTWORK_ID.fullmatch(artwork_id):
            raise HTTPException(400, "Invalid artwork ID.")
        settings = settings_loader()
        payload = ArtCatalogStore(settings.data_dir).thumbnail(artwork_id)
        if payload is None:
            raise HTTPException(404, "No cached preview is available for this artwork.")
        etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content=payload, media_type="image/webp", headers=headers)

    return router
