"""Tests for live score feeds, rendering, scheduling, and bounded TV storage."""

from __future__ import annotations

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
)


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
    created = _tracker(LiveScoreStore(tmp_path), provider="thesportsdb")
    reopened = LiveScoreStore(tmp_path)

    assert created["has_api_key"] is True
    assert "api_key" not in created
    assert reopened.get_tracker(created["id"], include_secret=True)["api_key"] == "private-key"
    assert reopened.due_tracker_ids()


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
