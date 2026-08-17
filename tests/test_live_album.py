"""Tests for public album adapters, scheduling, and bounded slideshow storage."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from frameart.live_album import (
    AlbumItem,
    LiveAlbumService,
    LiveAlbumStore,
    _immich_source,
    _manifest_items,
    _validate_network_target,
    extract_page_items,
    load_album_items,
)


def _image_bytes(color: str = "blue") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1600, 1200), color).save(output, format="JPEG")
    return output.getvalue()


def _album(store: LiveAlbumStore, **changes):
    values = {
        "name": "Family album",
        "provider": "manifest",
        "source_url": "https://photos.example/album.json?token=private",
        "group_id": "a" * 32,
        "interval_seconds": 300,
        "shuffle": False,
        "allow_private_network": False,
        "enabled": True,
    }
    values.update(changes)
    return store.create_album(**values)


def test_album_store_masks_source_and_persists_runtime(tmp_path: Path):
    store = LiveAlbumStore(tmp_path)
    created = _album(store)

    assert created["source_host"] == "photos.example"
    assert created["has_source_url"] is True
    assert "source_url" not in created
    assert "token=private" not in str(created)
    assert store.due_album_ids(now=created["next_advance"] + 1) == [created["id"]]
    assert store.get_album(created["id"], include_source=True)["source_url"].endswith(
        "token=private"
    )


def test_manifest_and_public_page_adapters_deduplicate_images():
    manifest = _manifest_items(
        {
            "photos": [
                {"id": "one", "url": "/one.jpg", "title": "One"},
                "https://cdn.example/two.png",
                "https://cdn.example/two.png",
            ]
        },
        "https://photos.example/album.json",
    )
    page = extract_page_items(
        '<meta property="og:image" content="/cover.jpg">'
        '<img src="https://cdn.example/photo.webp">'
        '<script>const image="https:\\/\\/cdn.example\\/embedded.jpeg"</script>',
        "https://photos.example/share/abc",
    )

    assert [item.item_id for item in manifest] == ["one", manifest[1].item_id]
    assert [item.image_url for item in manifest] == [
        "https://photos.example/one.jpg",
        "https://cdn.example/two.png",
    ]
    assert {item.image_url for item in page} == {
        "https://photos.example/cover.jpg",
        "https://cdn.example/photo.webp",
        "https://cdn.example/embedded.jpeg",
    }


@patch("frameart.live_album.socket.getaddrinfo")
def test_remote_url_guard_rejects_private_targets_by_default(mock_resolve):
    mock_resolve.return_value = [(2, 1, 6, "", ("192.168.1.20", 443))]

    with pytest.raises(ValueError, match="private or non-public"):
        _validate_network_target(
            "https://albums.internal/share", allow_private_network=False
        )
    assert _validate_network_target(
        "https://albums.internal/share", allow_private_network=True
    ).startswith("https://")


@patch("frameart.live_album.socket.getaddrinfo")
@patch("frameart.live_album.httpx.get")
def test_fetch_validates_redirect_before_following_it(mock_get, mock_resolve):
    public_record = (2, 1, 6, "", ("93.184.216.34", 443))
    private_record = (2, 1, 6, "", ("192.168.1.20", 443))
    mock_resolve.side_effect = [[public_record], [private_record]]
    mock_get.return_value.status_code = 302
    mock_get.return_value.headers = {"location": "https://albums.internal/private.jpg"}

    with pytest.raises(ValueError, match="private or non-public"):
        load_album_items(
            "manifest",
            "https://photos.example/album.json",
            allow_private_network=False,
        )
    assert mock_get.call_count == 1


def test_immich_source_and_adapter_use_shared_link_auth():
    api_base, candidates = _immich_source("https://immich.example/share/share-key")
    assert api_base == "https://immich.example/api"
    assert candidates[0] == ("x-immich-share-key", "share-key")
    payload = b'{"album":{"assets":[{"id":"asset-1","type":"IMAGE",' \
        b'"originalFileName":"Photo.jpg"},{"id":"video-1","type":"VIDEO"}]}}'

    with patch("frameart.live_album._get_bytes", return_value=(payload, "application/json", "")):
        items = load_album_items(
            "immich",
            "https://immich.example/share/share-key",
            allow_private_network=False,
        )

    assert len(items) == 1
    assert items[0].image_url == "https://immich.example/api/assets/asset-1/original"
    assert items[0].fallback_url.endswith("/thumbnail?size=preview")
    assert items[0].headers == {"x-immich-share-key": "share-key"}


@patch("frameart.live_album.IntegrationPublisher.publish", return_value=[])
@patch("frameart.live_album.replace_group_image")
@patch("frameart.live_album.download_album_image")
@patch("frameart.live_album.load_album_items")
def test_service_advances_and_overwrites_one_private_4k_file(
    mock_load,
    mock_download,
    mock_display,
    _publish,
    tmp_path,
):
    store = LiveAlbumStore(tmp_path)
    album = _album(store)
    mock_load.return_value = [
        AlbumItem("one", "https://cdn.example/one.jpg", "First"),
        AlbumItem("two", "https://cdn.example/two.jpg", "Second"),
    ]
    mock_download.side_effect = [_image_bytes("blue"), _image_bytes("red")]
    mock_display.side_effect = [
        ({"living": "content-1"}, {}, [{"content_id": "content-1"}], []),
        ({"living": "content-2"}, {}, [{"content_id": "content-2"}], []),
    ]
    settings = SimpleNamespace(data_dir=tmp_path, tvs={})
    service = LiveAlbumService(lambda: settings)

    first = service.advance_album(album["id"])
    second = service.advance_album(album["id"])
    current = tmp_path / "modes" / "live-album" / album["id"] / "current.png"

    assert first["item"]["id"] == "one"
    assert second["item"]["id"] == "two"
    assert list(current.parent.iterdir()) == [current]
    assert current.stat().st_mode & 0o777 == 0o600
    with Image.open(current) as image:
        assert image.size == (3840, 2160)
    persisted = store.get_album(album["id"])
    assert persisted["last_item_id"] == "two"
    assert persisted["current_content_ids"] == {"living": "content-2"}


@patch("frameart.live_album.delete_mode_tv_images")
def test_delete_album_removes_bounded_local_state(_delete_tv, tmp_path):
    store = LiveAlbumStore(tmp_path)
    album = _album(store)
    mode_dir = tmp_path / "modes" / "live-album" / album["id"]
    mode_dir.mkdir(parents=True)
    (mode_dir / "current.png").write_bytes(b"image")
    settings = SimpleNamespace(data_dir=tmp_path, tvs={})

    assert LiveAlbumService(lambda: settings).delete_album(album["id"]) is True
    assert not mode_dir.exists()
    assert store.get_album(album["id"]) is None
