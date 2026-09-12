"""Tests for durable groups, playlists, schedules, and integrations."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from frameart.automation import (
    AutomationScheduler,
    AutomationStore,
    IntegrationPublisher,
    display_artifact,
)


def _definitions(store: AutomationStore):
    group = store.create_group("Downstairs", ["living_room", "kitchen"])
    playlist = store.create_playlist("Landscapes", ["job-one", "job-two"])
    schedule = store.create_schedule(
        name="Evening",
        playlist_id=playlist["id"],
        group_id=group["id"],
        interval_seconds=30,
        matte="none",
        enabled=True,
    )
    return group, playlist, schedule


def test_automation_definitions_persist_and_cascade(tmp_path: Path):
    store = AutomationStore(tmp_path)
    group, playlist, schedule = _definitions(store)

    reopened = AutomationStore(tmp_path)
    assert reopened.get_group(group["id"])["tv_profile_ids"] == ["living_room", "kitchen"]
    assert reopened.get_playlist(playlist["id"])["job_ids"] == ["job-one", "job-two"]
    assert reopened.get_schedule(schedule["id"])["enabled"] is True
    assert (tmp_path / "frameart.sqlite3").stat().st_mode & 0o777 == 0o600

    assert reopened.delete_playlist(playlist["id"]) is True
    assert reopened.get_schedule(schedule["id"]) is None


@patch("frameart.automation.IntegrationPublisher.publish", return_value=[])
@patch("frameart.automation.display_artifact")
def test_scheduler_advances_playlist_and_records_partial_result(
    mock_display,
    _mock_publish,
    tmp_path,
):
    store = AutomationStore(tmp_path)
    _group, _playlist, schedule = _definitions(store)
    settings = SimpleNamespace(data_dir=tmp_path, tvs={})
    mock_display.side_effect = [
        {"tv_profile_id": "living_room", "content_id": "one"},
        RuntimeError("offline"),
    ]

    result = AutomationScheduler(lambda: settings).run_schedule(schedule["id"])

    assert result["job_id"] == "job-one"
    assert result["status"] == "partial"
    persisted = store.get_schedule(schedule["id"])
    assert persisted["current_index"] == 1
    assert persisted["last_status"] == "partial"
    assert "kitchen: offline" in persisted["last_error"]
    assert all(call.kwargs["require_art_mode"] is True for call in mock_display.call_args_list)


@patch("frameart.tv.controller.switch_art")
@patch("frameart.tv.controller.upload_image")
@patch("frameart.tv.controller.list_art_deduplicated")
@patch("frameart.tv.controller.get_status")
def test_scheduled_display_does_not_touch_tv_during_active_viewing(
    mock_get_status,
    mock_list,
    mock_upload,
    mock_switch,
    tmp_path,
):
    profile = MagicMock()
    settings = SimpleNamespace(data_dir=tmp_path, tvs={"living_room": profile})
    mock_get_status.return_value = SimpleNamespace(
        reachable=True, art_mode_supported=True, art_mode_on=False
    )

    with pytest.raises(RuntimeError, match="normal viewing was left untouched"):
        display_artifact(
            settings,
            "job-one",
            "living_room",
            "none",
            require_art_mode=True,
        )

    mock_list.assert_not_called()
    mock_upload.assert_not_called()
    mock_switch.assert_not_called()


@patch("frameart.automation.httpx.post")
def test_webhooks_are_signed_and_secrets_are_not_listed(mock_post, tmp_path):
    mock_post.return_value.raise_for_status.return_value = None
    store = AutomationStore(tmp_path)
    created = store.create_webhook(
        "Home Assistant",
        "http://homeassistant.local/webhook/frameart",
        ["integration.test"],
    )

    deliveries = IntegrationPublisher(store).publish("integration.test", {"ok": True})

    assert deliveries == [{"webhook_id": created["id"], "ok": True}]
    headers = mock_post.call_args.kwargs["headers"]
    assert headers["X-FrameArt-Event"] == "integration.test"
    assert headers["X-FrameArt-Signature"].startswith("sha256=")
    assert "secret" not in store.list_webhooks()[0]
    assert store.list_webhooks(include_secrets=True)[0]["secret"] == created["secret"]


def test_due_schedule_query_and_pause(tmp_path):
    store = AutomationStore(tmp_path)
    _group, _playlist, schedule = _definitions(store)
    assert store.due_schedule_ids(time.time() + 31) == [schedule["id"]]
    assert store.set_schedule_enabled(schedule["id"], False) is True
    assert store.due_schedule_ids(time.time() + 31) == []


def test_webhook_events_are_json_backed(tmp_path):
    store = AutomationStore(tmp_path)
    created = store.create_webhook(
        "Receiver",
        "https://example.test/frameart",
        ["schedule.completed", "schedule.failed"],
    )
    row = store.list_webhooks(include_secrets=True)[0]
    assert row["events"] == ["schedule.completed", "schedule.failed"]
    assert len(created["secret"]) == 64
    assert json.dumps(row)
