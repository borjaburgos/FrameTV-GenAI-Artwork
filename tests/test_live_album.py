"""Tests for public album adapters and deletion-aware TV synchronization."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest
from PIL import Image

from frameart.live_album import (
    AlbumItem,
    AlbumSnapshot,
    LiveAlbumService,
    LiveAlbumStore,
    download_album_image,
    fetch_icloud_album,
    parse_google_album_page,
    validate_album_url,
)


class FakeResponse:
    def __init__(self, payload=b"", *, status=200, headers=None):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        self.content = payload
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _jpeg_bytes(color: str = "blue") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1600, 1200), color).save(output, format="JPEG")
    return output.getvalue()


def _item(number: int) -> AlbumItem:
    return AlbumItem(
        item_id=f"photo-{number}",
        image_url=f"https://cdn{number}.icloud-content.com/photo.jpg",
        added_at=float(number),
        checksum=f"checksum-{number}",
        title=f"Photo {number}",
    )


def _album(store: LiveAlbumStore, **changes):
    values = {
        "name": "Family album",
        "provider": "icloud",
        "source_url": "https://www.icloud.com/sharedalbum/#D2Eabcdefgh",
        "target_type": "tv",
        "target_id": "living",
        "interval_seconds": 300,
        "display_newest": True,
        "enabled": True,
    }
    values.update(changes)
    return store.create_album(**values)


def test_provider_url_validation_keeps_icloud_fragment_and_limits_hosts():
    icloud = validate_album_url(
        "icloud", "https://www.icloud.com/sharedalbum/#D2Eabcdefgh"
    )
    google = validate_album_url(
        "google_photos", "https://photos.app.goo.gl/public-token"
    )

    assert icloud.endswith("#D2Eabcdefgh")
    assert google == "https://photos.app.goo.gl/public-token"
    with pytest.raises(ValueError, match="iCloud public Shared Album"):
        validate_album_url("icloud", "https://example.com/#D2Eabcdefgh")
    with pytest.raises(ValueError, match="public Google Photos"):
        validate_album_url("google_photos", "https://example.com/album")


@patch("frameart.live_album.httpx.post")
def test_icloud_adapter_selects_newest_ten_stills_and_resolves_largest_derivatives(
    mock_post,
):
    photos = []
    items = {}
    for number in range(12):
        small = f"small-{number}"
        large = f"large-{number}"
        photos.append(
            {
                "photoGuid": f"photo-{number}",
                "batchDateCreated": f"2026-09-{number + 1:02d}T12:00:00Z",
                "dateCreated": "2020-01-01T00:00:00Z",
                "caption": f"Photo {number}",
                "derivatives": {
                    "320": {"width": "320", "height": "200", "checksum": small},
                    "2048": {"width": "2048", "height": "1200", "checksum": large},
                },
            }
        )
        items[large] = {
            "url_location": "cdn.icloud-content.com",
            "url_path": f"/album/{number}.jpg?signature=public",
        }
    photos.append(
        {
            "photoGuid": "video-1",
            "mediaAssetType": "video",
            "batchDateCreated": "2026-12-31T12:00:00Z",
            "derivatives": {
                "PosterFrame": {"width": 320, "height": 200, "checksum": "video"}
            },
        }
    )
    mock_post.side_effect = [
        FakeResponse({"streamName": "Family", "photos": photos}),
        FakeResponse(
            {
                "locations": {
                    "cdn.icloud-content.com": {
                        "scheme": "https",
                        "hosts": ["cdn.icloud-content.com"],
                    }
                },
                "items": items,
            }
        ),
    ]

    snapshot = fetch_icloud_album(
        "https://www.icloud.com/sharedalbum/#D2Eabcdefgh"
    )

    assert snapshot.name == "Family"
    assert snapshot.total_count == 12
    assert len(snapshot.items) == 10
    assert [item.item_id for item in snapshot.items] == [
        f"photo-{number}" for number in range(11, 1, -1)
    ]
    assert snapshot.items[0].image_url.startswith(
        "https://cdn.icloud-content.com/album/11.jpg"
    )
    first_url = mock_post.call_args_list[0].args[0]
    assert first_url.startswith("https://p138-sharedstreams.icloud.com/")


def test_google_adapter_parses_public_page_sorts_by_album_add_date_and_skips_video():
    rows = [
        [
            "older",
            ["https://lh3.googleusercontent.com/pw/older", 4000, 3000],
            1000,
            "unused",
            0,
            2000,
        ],
        [
            "video",
            ["https://lh3.googleusercontent.com/pw/video", 1920, 1080],
            2000,
            "unused",
            0,
            4000,
            None,
            None,
            None,
            {"76647426": []},
        ],
        [
            "newer",
            ["https://lh3.googleusercontent.com/pw/newer", 3024, 4032],
            3000,
            "unused",
            0,
            5000,
        ],
    ]
    page = (
        '<meta property="og:title" content="Vacation">'
        "<script>AF_initDataCallback({key: 'ds:1', data:"
        + json.dumps([None, rows])
        + ", sideChannel: {}});</script>"
    )

    snapshot = parse_google_album_page(page)

    assert snapshot.name == "Vacation"
    assert snapshot.total_count == 2
    assert [item.item_id for item in snapshot.items] == ["newer", "older"]
    assert snapshot.items[0].image_url.endswith("=w3024-h2160")
    assert snapshot.items[0].added_at == 5.0


def test_google_adapter_accepts_a_confirmed_empty_album_but_rejects_changed_markup():
    empty = "<script>AF_initDataCallback({data:[null,[]]});</script>"
    assert parse_google_album_page(empty).items == ()
    with pytest.raises(RuntimeError, match="did not expose public album data"):
        parse_google_album_page("<html>No public data</html>")


@patch("frameart.live_album.httpx.get")
def test_photo_download_is_validated_resized_and_converted_to_jpeg(mock_get):
    mock_get.return_value = FakeResponse(
        _jpeg_bytes(), headers={"content-type": "image/jpeg"}
    )

    result = download_album_image("icloud", _item(1))

    assert result.startswith(b"\xff\xd8")
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (1600, 1200)


def test_store_masks_public_tokens_and_updates_tv_profile_references(tmp_path: Path):
    store = LiveAlbumStore(tmp_path)
    album = _album(store)

    assert album["source_host"] == "www.icloud.com"
    assert album["has_source_url"] is True
    assert "source_url" not in album
    assert "D2Eabcdefgh" not in str(album)
    assert store.due_album_ids(now=album["next_sync"] + 1) == [album["id"]]
    assert store.replace_tv_profile_ids({"living": "Living-TV"}) == 1
    assert store.get_album(album["id"])["tv_profile_id"] == "Living-TV"


@patch("frameart.live_album.IntegrationPublisher.publish", return_value=[])
@patch("frameart.tv.controller.delete_art", return_value=True)
@patch("frameart.tv.controller.switch_art", return_value=True)
@patch("frameart.tv.controller.upload_image")
@patch("frameart.live_album.download_album_image", return_value=_jpeg_bytes())
@patch("frameart.live_album.fetch_album_items")
def test_sync_reconciles_newest_ten_and_deletes_photo_removed_upstream(
    mock_fetch,
    _download,
    mock_upload,
    mock_switch,
    mock_delete,
    _publish,
    tmp_path,
):
    settings = SimpleNamespace(data_dir=tmp_path, tvs={"living": object()})
    store = LiveAlbumStore(tmp_path)
    album = _album(store)
    initial = tuple(_item(number) for number in range(9, -1, -1))
    replacement = AlbumItem(
        "photo-10",
        "https://cdn10.icloud-content.com/photo.jpg",
        10,
        "checksum-10",
        "Newest",
    )
    mock_fetch.side_effect = [
        AlbumSnapshot("Family", 12, initial),
        AlbumSnapshot("Family", 12, (replacement, *initial[:-1])),
    ]
    mock_upload.side_effect = [
        SimpleNamespace(success=True, content_id=f"content-{index}", error=None)
        for index in range(10)
    ] + [SimpleNamespace(success=True, content_id="content-10", error=None)]
    service = LiveAlbumService(lambda: settings)

    first = service.sync_album(album["id"])
    second = service.sync_album(album["id"])

    assert first["uploaded"] == 10
    assert second["uploaded"] == 1
    assert second["deleted"] == 1
    assert len(store.current_items(album["id"], "living")) == 10
    assert store.current_items(album["id"], "living")["photo-10"]["content_id"] == "content-10"
    assert mock_delete.call_args_list[-1] == call(settings.tvs["living"], ["content-9"])
    assert mock_switch.call_args_list[-1] == call(settings.tvs["living"], "content-10")


@patch("frameart.live_album.IntegrationPublisher.publish", return_value=[])
@patch("frameart.tv.controller.delete_art", return_value=True)
@patch("frameart.tv.controller.switch_art", return_value=True)
@patch("frameart.tv.controller.upload_image")
@patch("frameart.live_album.download_album_image", return_value=_jpeg_bytes())
@patch("frameart.live_album.fetch_album_items")
def test_confirmed_empty_album_removes_owned_photos(
    mock_fetch,
    _download,
    mock_upload,
    _switch,
    mock_delete,
    _publish,
    tmp_path,
):
    settings = SimpleNamespace(data_dir=tmp_path, tvs={"living": object()})
    store = LiveAlbumStore(tmp_path)
    album = _album(store)
    mock_fetch.side_effect = [
        AlbumSnapshot("Family", 1, (_item(1),)),
        AlbumSnapshot("Family", 0, ()),
    ]
    mock_upload.return_value = SimpleNamespace(
        success=True, content_id="content-1", error=None
    )
    service = LiveAlbumService(lambda: settings)

    service.sync_album(album["id"])
    emptied = service.sync_album(album["id"])

    assert emptied["deleted"] == 1
    assert store.current_items(album["id"], "living") == {}
    assert mock_delete.call_args_list[-1] == call(settings.tvs["living"], ["content-1"])


@patch("frameart.live_album.IntegrationPublisher.publish", return_value=[])
@patch("frameart.tv.controller.delete_art")
@patch("frameart.tv.controller.upload_image")
@patch("frameart.live_album.fetch_album_items")
def test_provider_failure_keeps_existing_tv_content(
    mock_fetch,
    mock_upload,
    mock_delete,
    _publish,
    tmp_path,
):
    settings = SimpleNamespace(data_dir=tmp_path, tvs={"living": object()})
    store = LiveAlbumStore(tmp_path)
    album = _album(store)
    store.upsert_current(album["id"], "living", _item(1), "content-1")
    mock_fetch.side_effect = RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        LiveAlbumService(lambda: settings).sync_album(album["id"])

    assert store.current_items(album["id"], "living")["photo-1"]["content_id"] == "content-1"
    mock_upload.assert_not_called()
    mock_delete.assert_not_called()


@patch("frameart.live_album.IntegrationPublisher.publish", return_value=[])
@patch("frameart.tv.controller.delete_art", return_value=False)
@patch("frameart.tv.controller.switch_art", return_value=True)
@patch("frameart.tv.controller.upload_image")
@patch("frameart.live_album.download_album_image", return_value=_jpeg_bytes())
@patch("frameart.live_album.fetch_album_items")
def test_failed_cleanup_is_retained_and_blocks_more_uploads(
    mock_fetch,
    _download,
    mock_upload,
    _switch,
    mock_delete,
    _publish,
    tmp_path,
):
    settings = SimpleNamespace(data_dir=tmp_path, tvs={"living": object()})
    store = LiveAlbumStore(tmp_path)
    album = _album(store)
    store.upsert_current(album["id"], "living", _item(1), "content-1")
    mock_fetch.side_effect = [
        AlbumSnapshot("Family", 1, (_item(2),)),
        AlbumSnapshot("Family", 1, (_item(3),)),
    ]
    mock_upload.return_value = SimpleNamespace(
        success=True, content_id="content-2", error=None
    )
    service = LiveAlbumService(lambda: settings)

    first = service.sync_album(album["id"])
    second = service.sync_album(album["id"])

    assert first["status"] == "partial"
    assert store.stale_ids(album["id"], "living") == ["content-1"]
    assert second["status"] == "error"
    assert second["uploaded"] == 0
    assert mock_upload.call_count == 1
    assert mock_delete.call_count == 2
