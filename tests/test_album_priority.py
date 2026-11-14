"""Family album events win over Daily Art without interrupting television."""

from __future__ import annotations

import io
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from frameart import daily_art, live_album
from frameart.art_delivery import CatalogDeliveryService, tv_delivery_lock
from frameart.automation import AutomationStore
from frameart.daily_art import DailyArtStore, DailyArtWorker
from frameart.live_album import AlbumItem, AlbumSnapshot, LiveAlbumService, LiveAlbumStore
from frameart.tv import controller


def photo(number, *, checksum=None, url=None):
    return AlbumItem(
        f"photo-{number}",
        url or f"https://cdn.icloud-content.com/{number}",
        float(number),
        checksum or f"version-{number}",
        f"Photo {number}",
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [datetime(2027, 1, 1, 15, tzinfo=timezone.utc)]
    monkeypatch.setattr(daily_art, "_now", lambda value=None: clock[0])
    settings = SimpleNamespace(
        data_dir=tmp_path,
        tvs={
            "family": SimpleNamespace(ip="192.0.2.20"),
            "bedroom": SimpleNamespace(ip="192.0.2.21"),
        },
    )
    albums = LiveAlbumStore(tmp_path)
    album = albums.create_album(
        name="Family",
        provider="icloud",
        source_url="https://www.icloud.com/sharedalbum/#synthetic",
        target_type="tv",
        target_id="family",
        interval_seconds=300,
        display_newest=True,
        enabled=True,
    )
    fetch = Mock(return_value=AlbumSnapshot("Family", 1, (photo(1),)))
    monkeypatch.setattr(live_album, "fetch_album_items", fetch)
    buffer = io.BytesIO()
    Image.new("RGB", (16, 9), "blue").save(buffer, "JPEG")
    monkeypatch.setattr(live_album, "download_album_image", Mock(return_value=buffer.getvalue()))
    status = Mock(
        return_value=SimpleNamespace(reachable=True, art_mode_supported=True, art_mode_on=True)
    )
    monkeypatch.setattr(controller, "get_status", status)
    counter = iter(range(1, 100))
    upload = Mock(
        side_effect=lambda *args, **kwargs: SimpleNamespace(
            success=True, content_id=f"content-{next(counter)}", error=None, deferred=False
        )
    )
    select = Mock(return_value=True)
    monkeypatch.setattr(controller, "upload_image", upload)
    monkeypatch.setattr(controller, "switch_art", select)
    monkeypatch.setattr(controller, "delete_art", Mock(return_value=True))
    monkeypatch.setattr(CatalogDeliveryService, "_fingerprint_matches", Mock(return_value=True))
    monkeypatch.setattr(CatalogDeliveryService, "_record_fingerprint", Mock(return_value=True))
    monkeypatch.setattr(live_album.IntegrationPublisher, "publish", Mock(return_value=[]))
    daily = DailyArtStore(tmp_path)
    daily.set_calendar(
        {
            "year": 2027,
            "timezone": "America/New_York",
            "entries": [
                {"date": f"2027-01-0{day}", "artwork_id": f"art-{day}"} for day in range(1, 4)
            ],
        }
    )
    daily.configure({"enabled": True, "target_id": "family", "change_time": "00:00"})
    return SimpleNamespace(
        clock=clock,
        settings=settings,
        albums=albums,
        album=album,
        fetch=fetch,
        upload=upload,
        select=select,
        status=status,
        daily=daily,
        service=LiveAlbumService(lambda: settings),
    )


def test_new_photo_priority_is_committed_before_upload_and_shown_before_other_photos(setup):
    state = setup
    state.fetch.return_value = AlbumSnapshot("Family", 2, (photo(2), photo(1)))
    order = []

    def uploaded(*args, **kwargs):
        hold = state.daily.snapshot()["holds"][0]
        assert hold["reason"] == "family-album"
        if not order:
            assert hold["pending"] is True
        assert kwargs["require_art_mode"] is True
        order.append("upload")
        return SimpleNamespace(success=True, content_id=f"content-{len(order)}", error=None)

    def selected(*args, **kwargs):
        order.append("select")
        assert kwargs["require_art_mode"] is True
        return True

    state.upload.side_effect = uploaded
    state.select.side_effect = selected
    state.service.sync_album(state.album["id"])
    assert order[:3] == ["upload", "select", "upload"]
    hold = state.daily.snapshot()["holds"][0]
    assert hold["pending"] is False
    assert hold["until"] == "2027-01-02T05:00:00+00:00"


def test_unchanged_poll_does_not_renew_hold_or_reselect_after_midnight(setup):
    state = setup
    state.service.sync_album(state.album["id"])
    state.upload.reset_mock()
    state.select.reset_mock()
    state.clock[0] = datetime(2027, 1, 2, 5, tzinfo=timezone.utc)
    state.service.sync_album(state.album["id"])
    assert state.daily.snapshot()["holds"] == []
    state.upload.assert_not_called()
    state.select.assert_not_called()
    calls = []
    DailyArtWorker(
        lambda: state.settings,
        lambda *args, **kwargs: (
            calls.append(args) or {"results": [{"tv": "family", "status": "displayed"}]}
        ),
    ).tick()
    assert len(calls) == 1


def test_busy_pending_survives_midnight_restart_then_expires_after_confirmed_selection(setup):
    state = setup
    state.status.return_value.art_mode_on = False
    state.service.sync_album(state.album["id"])
    state.upload.assert_not_called()
    state.select.assert_not_called()
    state.clock[0] = datetime(2027, 1, 2, 6, tzinfo=timezone.utc)
    reopened = DailyArtStore(state.settings.data_dir)
    assert reopened.snapshot()["holds"][0]["pending"] is True
    deliver = Mock()
    DailyArtWorker(lambda: state.settings, deliver).tick()
    deliver.assert_not_called()
    state.status.return_value.art_mode_on = True
    LiveAlbumService(lambda: state.settings).sync_album(state.album["id"])
    hold = reopened.snapshot()["holds"][0]
    assert hold["pending"] is False and hold["until"] == "2027-01-03T05:00:00+00:00"
    state.clock[0] = datetime(2027, 1, 3, 5, tzinfo=timezone.utc)
    assert reopened.snapshot()["holds"] == []


def test_pending_selection_retries_without_reupload_on_unchanged_poll(setup):
    state = setup
    state.select.return_value = False
    state.service.sync_album(state.album["id"])
    assert state.daily.snapshot()["holds"][0]["pending"] is True
    assert state.upload.call_count == 1
    state.select.return_value = True
    result = state.service.sync_album(state.album["id"])
    assert state.upload.call_count == 1
    assert state.select.call_count == 2
    assert result["results"][0]["displayed"] == 1
    assert state.daily.snapshot()["holds"][0]["pending"] is False


def test_newer_photo_replaces_busy_candidate_and_only_latest_is_selected(setup):
    state = setup
    state.status.return_value.art_mode_on = False
    state.service.sync_album(state.album["id"])
    state.fetch.return_value = AlbumSnapshot("Family", 2, (photo(2), photo(1)))
    state.service.sync_album(state.album["id"])
    state.status.return_value.art_mode_on = True
    state.service.sync_album(state.album["id"])
    latest = state.albums.current_items(state.album["id"], "family")["photo-2"]["content_id"]
    state.select.assert_called_once_with(
        state.settings.tvs["family"], latest, require_art_mode=True
    )


@pytest.mark.parametrize("change", ["rotated_url", "deleted_newest"])
def test_url_rotation_or_deletion_fallback_to_known_photo_is_not_a_new_event(setup, change):
    state = setup
    state.fetch.return_value = AlbumSnapshot("Family", 2, (photo(2), photo(1)))
    state.service.sync_album(state.album["id"])
    state.clock[0] = datetime(2027, 1, 2, 6, tzinfo=timezone.utc)
    state.select.reset_mock()
    state.fetch.return_value = (
        AlbumSnapshot("Family", 1, (photo(1),))
        if change == "deleted_newest"
        else (
            AlbumSnapshot(
                "Family",
                2,
                (photo(2, url="https://cdn.icloud-content.com/rotated?key=new"), photo(1)),
            )
        )
    )
    state.service.sync_album(state.album["id"])
    assert state.daily.snapshot()["holds"] == []
    state.select.assert_not_called()


def test_existing_feed_without_source_cache_baselines_without_reannouncing(setup):
    state = setup
    state.albums.upsert_current(state.album["id"], "family", photo(1), "known-existing")
    state.albums.update_runtime(state.album["id"], next_sync=0, status="synced", error=None)
    state.service.sync_album(state.album["id"])
    assert state.daily.snapshot()["holds"] == []
    state.select.assert_not_called()
    state.upload.assert_not_called()


def test_clear_manual_holds_never_clears_family_album_pending(setup):
    state = setup
    state.status.return_value.art_mode_on = False
    state.service.sync_album(state.album["id"])
    state.daily.note_manual_choice("family")
    state.daily.configure({"clear_holds": True})
    holds = state.daily.snapshot()["holds"]
    assert len(holds) == 1 and holds[0]["reason"] == "family-album" and holds[0]["pending"]


@pytest.mark.parametrize("action", ["pause", "delete"])
def test_explicitly_stopping_feed_releases_its_undeliverable_pending_photo(setup, action):
    state = setup
    state.status.return_value.art_mode_on = False
    state.service.sync_album(state.album["id"])
    assert state.daily.snapshot()["holds"][0]["pending"] is True
    if action == "pause":
        state.albums.set_enabled(state.album["id"], False)
    else:
        state.albums.delete_album(state.album["id"])
    assert state.daily.snapshot()["holds"] == []


def test_group_members_have_independent_pending_and_midnight_release(setup):
    state = setup
    group = AutomationStore(state.settings.data_dir).create_group("Both", ["family", "bedroom"])
    state.albums.update_album(
        state.album["id"],
        name="Family",
        provider="icloud",
        source_url="https://www.icloud.com/sharedalbum/#synthetic",
        target_type="group",
        target_id=group["id"],
        interval_seconds=300,
        display_newest=True,
        enabled=True,
    )
    state.status.side_effect = lambda profile: SimpleNamespace(
        reachable=True, art_mode_supported=True, art_mode_on=profile.ip == "192.0.2.20"
    )
    state.service.sync_album(state.album["id"])
    state.clock[0] = datetime(2027, 1, 2, 6, tzinfo=timezone.utc)
    holds = state.daily.snapshot()["holds"]
    assert [hold["tv"] for hold in holds] == ["bedroom"]
    state.daily.configure({"target_type": "group", "target_id": group["id"]})
    skips = []
    DailyArtWorker(
        lambda: state.settings,
        lambda *args, skip_tvs: (
            skips.append(skip_tvs) or {"results": [{"tv": "family", "status": "displayed"}]}
        ),
    ).tick()
    assert skips == [{"bedroom"}]


def test_priority_precedes_shared_tv_lock_and_family_selection_wins_inflight_daily(setup):
    state = setup
    event = threading.Event()
    original = state.service._cache_snapshot
    state.service._cache_snapshot = lambda *args: (event.set(), original(*args))[1]
    errors = []
    lock = tv_delivery_lock(state.settings.tvs["family"].ip)
    lock.acquire()
    thread = threading.Thread(target=lambda: state.service.sync_album(state.album["id"]))
    try:
        thread.start()
        assert event.wait(timeout=5)
        assert state.daily.snapshot()["holds"][0]["pending"] is True
        state.upload.assert_not_called()
        state.select.assert_not_called()
    finally:
        lock.release()
        thread.join(timeout=10)
    assert not thread.is_alive() and not errors
    state.select.assert_called_once()


def test_unknown_upload_is_not_blindly_repeated_while_candidate_pending(setup):
    state = setup
    state.upload.side_effect = RuntimeError("synthetic post-send response failure")
    state.service.sync_album(state.album["id"])
    state.service.sync_album(state.album["id"])
    assert state.upload.call_count == 1
    assert state.daily.snapshot()["holds"][0]["pending"] is True
    state.select.assert_not_called()


def test_guarded_pre_send_deferral_remains_retryable(setup):
    state = setup
    state.upload.side_effect = None
    state.upload.return_value = SimpleNamespace(success=False, content_id="", deferred=True)
    state.service.sync_album(state.album["id"])
    state.upload.return_value = SimpleNamespace(
        success=True, content_id="safe-upload", deferred=False
    )
    state.service.sync_album(state.album["id"])
    assert state.upload.call_count == 2
    assert state.daily.snapshot()["holds"][0]["pending"] is False


def test_recycled_legacy_content_id_is_not_reused_or_deleted(setup, monkeypatch):
    state = setup
    state.albums.upsert_current(state.album["id"], "family", photo(1), "RECYCLED-ID")
    monkeypatch.setattr(CatalogDeliveryService, "_fingerprint_matches", Mock(return_value=False))
    state.service.sync_album(state.album["id"])
    state.upload.assert_called_once()
    assert state.select.call_args.args[1] != "RECYCLED-ID"
    controller.delete_art.assert_not_called()


def test_unverified_stale_content_id_is_preserved_on_tv(setup, monkeypatch):
    state = setup
    state.albums.upsert_current(state.album["id"], "family", photo(1), "RECYCLED-ID")
    state.albums.mark_current_stale(state.album["id"], "family", photo(1).item_id)
    state.fetch.return_value = AlbumSnapshot("Family", 0, ())
    monkeypatch.setattr(CatalogDeliveryService, "_fingerprint_matches", Mock(return_value=False))
    result = state.service.sync_album(state.album["id"])
    controller.delete_art.assert_not_called()
    assert state.albums.all_owned_ids(state.album["id"], "family") == []
    assert result["deleted"] == 0


def test_unverifiable_fresh_upload_does_not_accumulate_duplicates_on_unchanged_polls(
    setup, monkeypatch
):
    state = setup
    monkeypatch.setattr(CatalogDeliveryService, "_fingerprint_matches", Mock(return_value=False))
    monkeypatch.setattr(CatalogDeliveryService, "_record_fingerprint", Mock(return_value=False))
    state.service.sync_album(state.album["id"])
    state.service.sync_album(state.album["id"])
    state.service.sync_album(state.album["id"])
    assert state.upload.call_count == 1
    assert state.select.call_count == 1
    controller.delete_art.assert_not_called()
