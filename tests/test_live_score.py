"""Tests for live score feeds, rendering, scheduling, and bounded TV storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from frameart.automation import AutomationStore
from frameart.live_score import (
    LiveScoreService,
    LiveScoreStore,
    ScoreEvent,
    TheSportsDBClient,
    render_scoreboard,
    sportsdb_api_key,
    sportsdb_api_key_source,
)
from frameart.settings_store import update_integration_secret


def _event(**changes):
    values = {
        "event_id": "game-1",
        "league": "Premier League",
        "league_id": "4328",
        "sport": "Soccer",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_team_id": "133604",
        "away_team_id": "133610",
        "home_score": "2",
        "away_score": "1",
        "status": "2H",
        "progress": "76'",
        "highlights": ["Goal — Arsenal, 74'"],
    }
    values.update(changes)
    return ScoreEvent(**values)


def _tracker(store: LiveScoreStore, **changes):
    values = {
        "name": "Match tracker",
        "provider": "manual",
        "api_key": "private-key",
        "tracking_kind": "game",
        "tracking_value": "game-1",
        "group_id": "a" * 32,
        "poll_seconds": 30,
        "refresh_seconds": 300,
        "theme": "dark",
        "enabled": True,
    }
    values.update(changes)
    return store.create_tracker(**values)


def test_tracker_persists_without_returning_api_key(tmp_path: Path):
    store = LiveScoreStore(tmp_path)
    created = _tracker(store, provider="thesportsdb")
    direct = _tracker(
        store,
        name="Kitchen tracker",
        group_id=None,
        tv_profile_id="kitchen",
    )
    reopened = LiveScoreStore(tmp_path)

    assert created["has_api_key"] is True
    assert "api_key" not in created
    assert created["target_type"] == "group"
    assert created["tv_profile_id"] is None
    assert direct["target_type"] == "tv"
    assert direct["group_id"] is None
    assert direct["tv_profile_id"] == "kitchen"
    assert reopened.get_tracker(created["id"], include_secret=True)["api_key"] == "private-key"
    assert reopened.due_tracker_ids()

    reopened.update_runtime(
        direct["id"],
        next_poll=0,
        last_status="displayed",
        last_error=None,
        current_content_ids={"kitchen": "current"},
        stale_content_ids={"kitchen": ["stale"]},
    )
    assert reopened.replace_tv_profile_ids({"kitchen": "Kitchen-TV"}) == 1
    renamed = reopened.get_tracker(direct["id"])
    assert renamed["tv_profile_id"] == "Kitchen-TV"
    assert renamed["current_content_ids"] == {"Kitchen-TV": "current"}
    assert renamed["stale_content_ids"] == {"Kitchen-TV": ["stale"]}


def test_shared_sportsdb_key_precedes_legacy_tracker_key_and_honors_environment(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("FRAMEART_THESPORTSDB_API_KEY", raising=False)
    monkeypatch.delenv("THESPORTSDB_API_KEY", raising=False)
    update_integration_secret(tmp_path, "thesportsdb", "managed-key")

    assert sportsdb_api_key(tmp_path, "legacy-tracker-key") == "managed-key"
    assert sportsdb_api_key_source(tmp_path) == "managed"

    monkeypatch.setenv("FRAMEART_THESPORTSDB_API_KEY", "environment-key")
    assert sportsdb_api_key(tmp_path, "legacy-tracker-key") == "environment-key"
    assert sportsdb_api_key_source(tmp_path) == "environment"


def test_existing_group_tracker_schema_is_migrated(tmp_path: Path):
    database_path = tmp_path / "frameart.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE live_score_trackers (
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
            INSERT INTO live_score_trackers (
                id, name, provider, tracking_kind, tracking_value, group_id,
                poll_seconds, refresh_seconds, theme, enabled, next_poll,
                current_content_ids, stale_content_ids, created_at
            ) VALUES (
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'Existing tracker', 'manual',
                'game', 'game-1', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                30, 300, 'dark', 1, 0, '{}', '{}', 0
            );
            """
        )

    tracker = LiveScoreStore(tmp_path).get_tracker("a" * 32)

    assert tracker["target_type"] == "group"
    assert tracker["target_id"] == "b" * 32
    assert tracker["group_id"] == "b" * 32
    assert tracker["tv_profile_id"] is None


@patch("frameart.live_score.httpx.get")
def test_sportsdb_adapter_normalizes_and_filters_team(mock_get):
    mock_get.return_value.json.return_value = {
        "livescores": [
            {
                "idEvent": "game-1",
                "strLeague": "Premier League",
                "strSport": "Soccer",
                "idHomeTeam": "133604",
                "idAwayTeam": "133610",
                "strHomeTeam": "Arsenal",
                "strAwayTeam": "Chelsea",
                "intHomeScore": "2",
                "intAwayScore": "1",
                "strStatus": "2H",
                "strProgress": "76'",
            }
        ]
    }

    event = TheSportsDBClient("premium").fetch("team", "133604")

    assert event.home_team == "Arsenal"
    assert event.home_score == "2"
    assert mock_get.call_args.args[0].endswith("/livescore/all")
    assert mock_get.call_args.kwargs["headers"]["X-API-KEY"] == "premium"


@patch("frameart.live_score.httpx.get")
def test_sportsdb_adapter_accepts_all_supported_response_shapes(mock_get):
    row = {
        "idEvent": "game-1",
        "idLeague": "4481",
        "strLeague": "Europa League",
        "strSport": "Soccer",
        "strHomeTeam": "Arsenal",
        "strAwayTeam": "Roma",
        "strStatus": "1H",
    }
    payloads = [
        [row],
        {"livescore": [row]},
        {"livescores": [row]},
        {"events": [row]},
        {"data": [row]},
    ]

    for payload in payloads:
        mock_get.return_value.json.return_value = payload
        event = TheSportsDBClient("premium").fetch("league", "4481")
        assert event is not None
        assert event.event_id == "game-1"


@patch("frameart.live_score.httpx.get")
def test_sportsdb_adapter_prefers_first_active_match(mock_get):
    def row(event_id: str, status: str) -> dict[str, str]:
        return {
            "idEvent": event_id,
            "idLeague": "4481",
            "strLeague": "Europa League",
            "strSport": "Soccer",
            "strHomeTeam": "Home",
            "strAwayTeam": "Away",
            "strStatus": status,
        }

    mock_get.return_value.json.return_value = {
        "livescore": [
            row("finished", "FT"),
            row("scheduled", "NS"),
            row("active-second-half", "2H"),
            row("active-first-half", "1H"),
        ]
    }

    event = TheSportsDBClient("premium").fetch("league", "4481")

    assert event is not None
    assert event.event_id == "active-second-half"
    assert event.status == "2H"


def test_scoreboard_renderer_outputs_private_4k_image(tmp_path: Path):
    path = render_scoreboard(_event(), tmp_path / "current.png", theme="stadium")

    with Image.open(path) as image:
        assert image.size == (3840, 2160)
        assert image.mode == "RGB"
    assert path.stat().st_mode & 0o777 == 0o600


@patch("frameart.live_score.IntegrationPublisher.publish", return_value=[])
@patch.object(LiveScoreService, "_display")
def test_service_renders_on_change_and_skips_unchanged_event(mock_display, _publish, tmp_path):
    store = LiveScoreStore(tmp_path)
    tracker = _tracker(store)
    settings = SimpleNamespace(data_dir=tmp_path, tvs={})
    mock_display.return_value = (
        {"living_room": "new-content"},
        {},
        [{"tv_profile_id": "living_room", "content_id": "new-content"}],
        [],
    )
    service = LiveScoreService(lambda: settings)

    displayed = service.process_event(tracker["id"], _event())
    unchanged = service.process_event(tracker["id"], _event())

    assert displayed["status"] == "displayed"
    assert unchanged["status"] == "unchanged"
    assert mock_display.call_count == 1
    assert (tmp_path / "modes" / "live-score" / tracker["id"] / "current.png").is_file()


@patch("frameart.live_score.IntegrationPublisher.publish", return_value=[])
@patch.object(LiveScoreService, "_display")
@patch("frameart.live_score.httpx.get")
def test_live_provider_response_advances_waiting_tracker_to_displayed(
    mock_get,
    mock_display,
    _publish,
    tmp_path,
):
    store = LiveScoreStore(tmp_path)
    tracker = _tracker(
        store,
        provider="thesportsdb",
        tracking_kind="league",
        tracking_value="4481",
        group_id=None,
        tv_profile_id="score_tv",
    )
    settings = SimpleNamespace(data_dir=tmp_path, tvs={})
    waiting_response = MagicMock()
    waiting_response.json.return_value = {"livescore": []}
    live_response = MagicMock()
    live_response.json.return_value = {
        "livescore": [
            {
                "idEvent": "finished",
                "idLeague": "4481",
                "strLeague": "Europa League",
                "strSport": "Soccer",
                "strHomeTeam": "Finished Home",
                "strAwayTeam": "Finished Away",
                "strStatus": "FT",
            },
            {
                "idEvent": "active",
                "idLeague": "4481",
                "strLeague": "Europa League",
                "strSport": "Soccer",
                "strHomeTeam": "Active Home",
                "strAwayTeam": "Active Away",
                "intHomeScore": "1",
                "intAwayScore": "0",
                "strStatus": "2H",
                "strProgress": "58",
            },
        ]
    }
    mock_get.side_effect = [waiting_response, live_response]
    mock_display.return_value = (
        {"score_tv": "active-content"},
        {},
        [{"tv_profile_id": "score_tv", "content_id": "active-content"}],
        [],
    )
    service = LiveScoreService(lambda: settings)

    waiting = service.refresh_tracker(tracker["id"])
    displayed = service.refresh_tracker(tracker["id"], force=True)

    assert waiting["status"] == "waiting"
    assert displayed["status"] == "displayed"
    assert displayed["event"]["event_id"] == "active"
    persisted = store.get_tracker(tracker["id"])
    assert persisted["last_status"] == "displayed"
    assert persisted["last_event"]["event_id"] == "active"
    mock_display.assert_called_once()


@patch("frameart.tv.controller.delete_art", return_value=True)
@patch("frameart.tv.controller.switch_art", return_value=True)
@patch("frameart.tv.controller.upload_image")
def test_display_uploads_then_deletes_previous_tv_image(
    mock_upload,
    mock_switch,
    mock_delete,
    tmp_path,
):
    group = AutomationStore(tmp_path).create_group("Living Room", ["living_room"])
    profile = MagicMock()
    settings = SimpleNamespace(data_dir=tmp_path, tvs={"living_room": profile})
    image_path = tmp_path / "score.png"
    Image.new("RGB", (16, 9), "blue").save(image_path)
    mock_upload.return_value = SimpleNamespace(success=True, content_id="new", error=None)
    tracker = {
        "group_id": group["id"],
        "current_content_ids": {"living_room": "old"},
        "stale_content_ids": {},
    }

    current, stale, results, errors = LiveScoreService._display(
        settings,
        tracker,
        image_path,
    )

    assert current == {"living_room": "new"}
    assert stale == {}
    assert results[0]["content_id"] == "new"
    assert errors == []
    mock_switch.assert_called_once_with(profile, "new")
    mock_delete.assert_called_once_with(profile, ["old"])


@patch("frameart.tv.controller.delete_art", return_value=True)
@patch("frameart.tv.controller.switch_art", return_value=True)
@patch("frameart.tv.controller.upload_image")
def test_display_targets_one_individual_tv(
    mock_upload,
    mock_switch,
    mock_delete,
    tmp_path,
):
    profile = MagicMock()
    settings = SimpleNamespace(data_dir=tmp_path, tvs={"living_room": profile})
    image_path = tmp_path / "score.png"
    Image.new("RGB", (16, 9), "blue").save(image_path)
    mock_upload.return_value = SimpleNamespace(success=True, content_id="new", error=None)
    tracker = {
        "group_id": None,
        "tv_profile_id": "living_room",
        "current_content_ids": {"living_room": "old"},
        "stale_content_ids": {},
    }

    current, stale, results, errors = LiveScoreService._display(
        settings,
        tracker,
        image_path,
    )

    assert current == {"living_room": "new"}
    assert stale == {}
    assert results == [{"tv_profile_id": "living_room", "content_id": "new"}]
    assert errors == []
    mock_switch.assert_called_once_with(profile, "new")
    mock_delete.assert_called_once_with(profile, ["old"])


@patch("frameart.tv.controller.delete_art", return_value=True)
@patch("frameart.tv.controller.switch_art", return_value=False)
@patch("frameart.tv.controller.upload_image")
def test_display_cleans_up_new_upload_when_switch_fails(
    mock_upload,
    _mock_switch,
    mock_delete,
    tmp_path,
):
    group = AutomationStore(tmp_path).create_group("Living Room", ["living_room"])
    profile = MagicMock()
    settings = SimpleNamespace(data_dir=tmp_path, tvs={"living_room": profile})
    image_path = tmp_path / "score.png"
    Image.new("RGB", (16, 9), "blue").save(image_path)
    mock_upload.return_value = SimpleNamespace(success=True, content_id="orphan", error=None)
    tracker = {
        "group_id": group["id"],
        "current_content_ids": {"living_room": "old"},
        "stale_content_ids": {},
    }

    current, stale, results, errors = LiveScoreService._display(
        settings,
        tracker,
        image_path,
    )

    assert current == {"living_room": "old"}
    assert stale == {}
    assert results == []
    assert "did not switch" in errors[0]
    mock_delete.assert_called_once_with(profile, ["orphan"])
