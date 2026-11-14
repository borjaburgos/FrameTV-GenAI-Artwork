"""Queued catalog actions, strict calendars and automatic-mode API safety."""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from frameart import art_catalog, art_delivery, catalog_sync
from frameart.art_catalog import ArtCatalogStore, ArtworkMetadata
from frameart.art_catalog_api import create_art_catalog_router
from frameart.art_delivery import CatalogDeliveryService
from frameart.automation import AutomationStore
from frameart.config import Settings
from frameart.daily_art import DailyArtStore
from frameart.live_album import LiveAlbumStore
from frameart.live_score import LiveScoreStore
from frameart.resourcespace import ResourceSpaceError


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEART_DATA_DIR", str(tmp_path))
    return Settings(
        data_dir=tmp_path,
        resourcespace_url="http://catalog.local",
        resourcespace_user="reader",
        resourcespace_api_key="private-reader-key",
        resourcespace_collection=3,
        tvs={"family-room": {"ip": "10.0.0.2"}, "bedroom": {"ip": "10.0.0.3"}},
    )


@pytest.fixture
def catalog(settings):
    record = ArtworkMetadata(
        id="water-lilies",
        title="Water Lilies",
        artist={"id": "monet", "name": "Claude Monet"},
        rendition_ref=7,
    )
    store = ArtCatalogStore(settings.data_dir)
    store.replace([(record, b"cached-preview")], settings)
    return store


@pytest.fixture
def client(settings, catalog):
    app = FastAPI()
    app.include_router(create_art_catalog_router(lambda: settings))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def controlled_sync(monkeypatch):
    pending = []

    class Thread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            pending.append(self.target)

    monkeypatch.setattr(catalog_sync, "threading", SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(catalog_sync, "_jobs", {})
    return pending


def calendar():
    return {
        "year": 2027,
        "timezone": "America/New_York",
        "entries": [
            {
                "date": "2027-01-01",
                "artwork_id": "water-lilies",
                "month": 1,
                "day_of_year": 1,
                "season": "winter",
                "occasion": "New Year",
            }
        ],
    }


def test_refresh_queues_once_and_exposes_deterministic_progress(
    client, settings, controlled_sync, monkeypatch
):
    calls = []

    def refresh(settings, progress):
        calls.append(settings)
        progress(0, 365)
        assert client.get("/art-catalog").json()["sync"]["total"] == 365
        progress(200, 365)
        assert client.get("/art-catalog").json()["sync"]["completed"] == 200
        return {"item_count": 365}

    monkeypatch.setattr(catalog_sync, "sync_catalog", refresh)
    first = client.post("/art-catalog/sync")
    second = client.post("/art-catalog/sync")
    assert first.status_code == second.status_code == 202
    assert (
        first.json()
        == second.json()
        == {
            "status": "running",
            "completed": 0,
            "total": 0,
            "error": None,
        }
    )
    assert first.headers["cache-control"] == "no-store"
    assert calls == []
    assert len(controlled_sync) == 1
    controlled_sync.pop()()
    result = client.get("/art-catalog").json()
    assert result["sync"] == {
        "status": "completed",
        "completed": 365,
        "total": 365,
        "error": None,
    }
    assert result["item_count"] == 1  # Worker result metadata does not replace the cache itself.
    assert len(calls) == 1


@pytest.mark.parametrize("safe_error", [False, True])
def test_queued_refresh_failure_does_not_leak_secrets_or_clear_cache(
    client, controlled_sync, monkeypatch, safe_error
):
    before = client.get("/art-catalog").json()

    def fail(*args, **kwargs):
        if safe_error:
            raise ResourceSpaceError("ResourceSpace returned an incomplete collection page.")
        raise RuntimeError("private-reader-key https://private/upstream?access_key=SECRET")

    monkeypatch.setattr(catalog_sync, "sync_catalog", fail)
    assert client.post("/art-catalog/sync").status_code == 202
    controlled_sync.pop()()
    after = client.get("/art-catalog").json()
    assert after["sync"]["status"] == "error"
    assert after["items"] == before["items"]
    assert "private-reader-key" not in json.dumps(after)
    assert "access_key" not in json.dumps(after)


def test_send_only_enqueues_and_job_is_local(client, settings, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("API enqueue/status must never contact ResourceSpace or a TV")

    monkeypatch.setattr(art_delivery, "ResourceSpaceClient", forbidden)
    monkeypatch.setattr(CatalogDeliveryService, "_deliver_targets", forbidden)
    response = client.post(
        "/art-catalog/water-lilies/send",
        json={
            "target_type": "tv",
            "target_id": "family-room",
        },
    )
    assert response.status_code == 202
    queued = response.json()
    assert queued["status"] == "queued"
    assert queued["targets"] == ["family-room"]
    assert queued["results"] == []
    status = client.get("/art-catalog/deliveries/" + queued["id"])
    assert status.status_code == 200
    assert status.json() == queued
    assert status.headers["cache-control"] == "no-store"
    assert client.get("/art-catalog/deliveries/invalid").status_code == 400
    assert client.get("/art-catalog/deliveries/" + "0" * 32).status_code == 404
    assert (
        client.post(
            "/art-catalog/missing/send",
            json={
                "target_type": "tv",
                "target_id": "family-room",
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/art-catalog/water-lilies/send",
            json={
                "target_type": "tv",
                "target_id": "missing",
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/art-catalog/water-lilies/send",
            json={
                "target_type": "tv",
                "target_id": "family-room",
                "ip": "10.0.0.99",
            },
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "false"},
        {"enabled": None},
        {"clear_holds": 1},
        {"timezone": "Not/AZone"},
        {"target_id": []},
        {"target_id": "../anywhere"},
        {"target_type": "ip"},
        {"change_time": "24:00"},
        {"hold_until": "2027-01-01T12:00:00"},
        {"unexpected": "SECRET"},
    ],
)
def test_invalid_config_is_atomic_and_cannot_produce_500(client, settings, patch):
    before = DailyArtStore(settings.data_dir).config()
    response = client.put("/modes/daily-art", json=patch)
    assert response.status_code == 400
    assert "SECRET" not in response.text
    assert DailyArtStore(settings.data_dir).config() == before


def test_config_requires_calendar_and_real_target_but_can_disable_a_removed_target(
    client, settings
):
    assert client.put("/modes/daily-art", json={"enabled": True}).status_code == 400
    assert (
        client.put(
            "/modes/daily-art",
            json={
                "enabled": True,
                "target_id": "missing",
            },
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/modes/daily-art",
            json={
                "enabled": True,
                "target_id": "family-room",
            },
        ).status_code
        == 409
    )
    assert client.post("/modes/daily-art/calendar", json=calendar()).status_code == 200
    response = client.put(
        "/modes/daily-art",
        json={
            "enabled": True,
            "target_id": "family-room",
            "change_time": "10:15",
        },
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    settings.tvs.pop("family-room")
    paused = client.put("/modes/daily-art", json={"enabled": False})
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False
    assert paused.json()["change_time"] == "10:15"


@pytest.mark.parametrize("mode", ["album", "sports"])
@pytest.mark.parametrize("group_target", [False, True])
@pytest.mark.parametrize("feed_group_target", [False, True])
def test_overlapping_enabled_modes_conflict_without_disabling_anything(
    client, settings, mode, group_target, feed_group_target
):
    assert client.post("/modes/daily-art/calendar", json=calendar()).status_code == 200
    group = AutomationStore(settings.data_dir).create_group(
        "All frames", ["family-room", "bedroom"]
    )
    if mode == "album":
        other = LiveAlbumStore(settings.data_dir)
        feed = other.create_album(
            name="Family",
            provider="icloud",
            source_url="https://www.icloud.com/sharedalbum/#test",
            target_type="group" if feed_group_target else "tv",
            target_id=group["id"] if feed_group_target else "family-room",
            interval_seconds=300,
            display_newest=True,
            enabled=True,
        )
    else:
        other = LiveScoreStore(settings.data_dir)
        feed = other.create_tracker(
            name="Sports",
            provider="thesportsdb",
            api_key=None,
            tracking_kind="team",
            tracking_value="123",
            poll_seconds=60,
            refresh_seconds=60,
            theme="dark",
            enabled=True,
            **(
                {"group_id": group["id"]} if feed_group_target else {"tv_profile_id": "family-room"}
            ),
        )
    target = (
        {"target_type": "group", "target_id": group["id"]}
        if group_target
        else {
            "target_type": "tv",
            "target_id": "family-room",
        }
    )
    response = client.put("/modes/daily-art", json={"enabled": True, **target})
    assert response.status_code == (200 if mode == "album" else 409)
    assert DailyArtStore(settings.data_dir).config()["enabled"] is (mode == "album")
    assert (other.list_albums() if mode == "album" else other.list_trackers())[0]["enabled"] is True
    other.set_enabled(feed["id"], False)
    assert client.put("/modes/daily-art", json={"enabled": True, **target}).status_code == 200


def test_nonoverlapping_enabled_mode_does_not_block(client, settings):
    assert client.post("/modes/daily-art/calendar", json=calendar()).status_code == 200
    LiveAlbumStore(settings.data_dir).create_album(
        name="Bedroom album",
        provider="icloud",
        source_url="https://www.icloud.com/sharedalbum/#test",
        target_type="tv",
        target_id="bedroom",
        interval_seconds=300,
        display_newest=True,
        enabled=True,
    )
    assert (
        client.put(
            "/modes/daily-art",
            json={
                "enabled": True,
                "target_type": "tv",
                "target_id": "family-room",
            },
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(year=True),
        lambda data: data.update(year="2027"),
        lambda data: data.update(timezone="Not/AZone"),
        lambda data: data.update(entries=[]),
        lambda data: data.update(unknown=True),
        lambda data: data["entries"][0].update(month=True),
        lambda data: data["entries"][0].update(date="2027-02-30"),
        lambda data: data["entries"][0].update(date="2026-01-01"),
        lambda data: data["entries"][0].update(day_of_year=2),
        lambda data: data["entries"][0].update(artwork_id=[]),
        lambda data: data["entries"][0].update(unknown=True),
        lambda data: data["entries"].append(copy.deepcopy(data["entries"][0])),
    ],
)
def test_calendar_schema_is_strict_and_preserves_previous_import(client, settings, mutate):
    assert client.post("/modes/daily-art/calendar", json=calendar()).status_code == 200
    before = DailyArtStore(settings.data_dir).snapshot()
    invalid = calendar()
    mutate(invalid)
    response = client.post("/modes/daily-art/calendar", json=invalid)
    assert response.status_code == 400
    assert DailyArtStore(settings.data_dir).snapshot() == before


def test_calendar_requires_cached_ids_and_never_queries_upstream(client, settings, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Calendar validation must remain offline")

    monkeypatch.setattr(art_delivery, "ResourceSpaceClient", forbidden)
    payload = calendar()
    payload["entries"][0]["artwork_id"] = "not-cached"
    assert client.post("/modes/daily-art/calendar", json=payload).status_code == 409
    assert DailyArtStore(settings.data_dir).snapshot()["calendar_count"] == 0
    assert client.post("/modes/daily-art/calendar", json=calendar()).status_code == 200
    assert DailyArtStore(settings.data_dir).config()["enabled"] is False


@pytest.mark.parametrize(
    "raw",
    [
        b"not JSON",
        b"[]",
        b"null",
        b'{"year":2027,"year":2026,"entries":[]}',
        b'{"year":NaN,"entries":[]}',
    ],
)
def test_calendar_rejects_invalid_json_and_ambiguous_objects(client, raw):
    assert client.post("/modes/daily-art/calendar", content=raw).status_code == 400


def test_calendar_enforces_actual_stream_limit_not_declared_content_length(client, settings):
    response = client.post(
        "/modes/daily-art/calendar",
        headers={"Content-Length": "1"},
        content=iter([b" " * (256 * 1024), b" " * (256 * 1024), b"x"]),
    )
    assert response.status_code == 413
    assert DailyArtStore(settings.data_dir).snapshot()["calendar_count"] == 0


def test_calendar_accepts_exactly_512_kib_of_valid_json(client):
    raw = json.dumps(calendar()).encode()
    raw += b" " * (512 * 1024 - len(raw))
    response = client.post("/modes/daily-art/calendar", content=raw)
    assert response.status_code == 200
    assert response.json()["calendar_count"] == 1


def test_full_year_api_sync_import_enable_and_enqueue_are_offline_and_nonblocking(
    client, controlled_sync, monkeypatch
):
    class FullYearReader:
        def __init__(self, *args, **kwargs):
            pass

        def collection_resources(self, collection):
            return [{"ref": ref} for ref in range(1, 366)]

        def resource_fields(self, ref):
            return {
                "frameart_metadata": json.dumps(
                    {
                        "id": f"art-{ref}",
                        "title": f"Artwork {ref}",
                        "artist": {"id": "artist", "name": "Artist"},
                    }
                )
            }

        def thumbnail(self, ref, extension):
            return b"synthetic-preview"

    def no_delivery(*args, **kwargs):
        raise AssertionError("Queued API actions must never run TV delivery inline")

    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", FullYearReader)
    monkeypatch.setattr(art_catalog, "_webp_thumbnail", lambda payload: b"cached-preview")
    monkeypatch.setattr(CatalogDeliveryService, "_deliver_targets", no_delivery)
    assert client.post("/art-catalog/sync").status_code == 202
    assert client.get("/art-catalog").json()["item_count"] == 1
    controlled_sync.pop()()
    refreshed = client.get("/art-catalog").json()
    assert refreshed["item_count"] == 365
    assert refreshed["sync"]["status"] == "completed"
    annual = {
        "year": 2027,
        "timezone": "America/New_York",
        "entries": [
            {
                "date": (date(2027, 1, 1) + timedelta(days=number)).isoformat(),
                "artwork_id": f"art-{number + 1}",
            }
            for number in range(365)
        ],
    }
    assert client.post("/modes/daily-art/calendar", json=annual).json()["calendar_count"] == 365
    assert (
        client.put(
            "/modes/daily-art",
            json={
                "enabled": True,
                "target_id": "family-room",
            },
        ).json()["enabled"]
        is True
    )
    job = client.post(
        "/art-catalog/art-365/send",
        json={
            "target_type": "tv",
            "target_id": "family-room",
        },
    )
    assert job.status_code == 202
    assert client.get("/art-catalog/deliveries/" + job.json()["id"]).json()["status"] == "queued"


def test_full_app_job_and_daily_routes_require_correct_scopes(settings, catalog, monkeypatch):
    from frameart import api

    secured = settings.model_copy(
        update={
            "auth_enabled": True,
            "admin_token": SecretStr("test-admin"),
            "automation_token": SecretStr("test-control"),
            "trusted_lan_cidrs": [],
            "tailscale_auth_enabled": False,
            "api_rate_limit_per_minute": 10000,
        }
    )
    monkeypatch.setattr(api, "load_settings", lambda: secured)
    monkeypatch.setattr(api._catalog_delivery, "settings_loader", lambda: secured)
    client = TestClient(api.app)  # No lifespan: do not start any delivery/scheduler workers.
    admin = {"Authorization": "Bearer test-admin"}
    control = {"Authorization": "Bearer test-control"}
    assert client.get("/modes/daily-art").status_code == 401
    assert client.put("/modes/daily-art", json={"enabled": False}).status_code == 401
    assert client.get("/modes/daily-art", headers=control).status_code == 200
    assert (
        client.put("/modes/daily-art", headers=control, json={"enabled": False}).status_code == 403
    )
    assert (
        client.post("/modes/daily-art/calendar", headers=control, json=calendar()).status_code
        == 403
    )
    assert (
        client.post("/modes/daily-art/calendar", headers=admin, json=calendar()).status_code == 200
    )
    queued = client.post(
        "/art-catalog/water-lilies/send",
        headers=control,
        json={
            "target_type": "tv",
            "target_id": "family-room",
        },
    )
    assert queued.status_code == 202
    path = "/art-catalog/deliveries/" + queued.json()["id"]
    assert client.get(path).status_code == 401
    assert client.get(path, headers=control).status_code == 200
    assert client.get(path, headers=admin).status_code == 200
