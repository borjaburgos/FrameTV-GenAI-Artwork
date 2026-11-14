"""Recover delayed album identity evidence without repeating any upload."""

import io
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from frameart import live_album
from frameart.album_verification import thumbnail_matches_photo
from frameart.art_delivery import CatalogDeliveryStore
from frameart.live_album import AlbumItem, AlbumSnapshot, LiveAlbumService, LiveAlbumStore
from frameart.tv import controller


def jpeg(image, quality=94):
    output = io.BytesIO()
    image.save(output, "JPEG", quality=quality)
    return output.getvalue()


def photo():
    image = Image.new("RGB", (640, 480), "white")
    draw = ImageDraw.Draw(image)
    for i in range(32):
        draw.rectangle((i * 20, 0, i * 20 + 20, 480), fill=(i * 7, 240 - i * 5, i * 3))
        draw.ellipse((i * 16, i * 10, i * 16 + 100, i * 10 + 120), fill=(200, i * 7, 40))
    return image.filter(ImageFilter.GaussianBlur(2))


@pytest.mark.parametrize("padded", [False, True])
def test_source_comparison_accepts_scaled_jpeg_but_not_a_recycled_image(padded):
    original = photo()
    thumb = ImageOps.pad(original, (320, 180)) if padded else original.resize((240, 180))
    assert thumbnail_matches_photo(jpeg(original), jpeg(thumb, 85))
    assert not thumbnail_matches_photo(jpeg(original), jpeg(ImageOps.mirror(thumb)))
    altered = thumb.copy()
    ImageDraw.Draw(altered).rectangle((100, 50, 160, 100), fill="black")
    assert not thumbnail_matches_photo(jpeg(original), jpeg(altered))


@pytest.mark.parametrize("invalid", [b"", b"not-an-image", b"x" * (8 * 1024**2 + 1)])
def test_source_comparison_rejects_invalid_thumbnails(invalid):
    assert not thumbnail_matches_photo(jpeg(photo()), invalid)


def test_source_comparison_rejects_low_detail_tiny_and_arbitrarily_cropped_images():
    flat = jpeg(Image.new("RGB", (320, 180), "blue"))
    assert not thumbnail_matches_photo(flat, flat)
    original = photo()
    assert not thumbnail_matches_photo(jpeg(original), jpeg(original.resize((48, 36))))
    assert not thumbnail_matches_photo(jpeg(original), jpeg(original.crop((80, 60, 560, 420))))


@pytest.fixture
def state(tmp_path, monkeypatch):
    token = tmp_path / "pairing.token"
    token.write_text("synthetic-original-pairing")
    profile = SimpleNamespace(ip="192.0.2.10", token_file=str(token))
    settings = SimpleNamespace(data_dir=tmp_path, tvs={"family": profile})
    store = LiveAlbumStore(tmp_path)
    CatalogDeliveryStore(tmp_path)
    album = store.create_album(
        name="Family",
        provider="icloud",
        source_url="https://www.icloud.com/sharedalbum/#synthetic",
        target_type="tv",
        target_id="family",
        interval_seconds=300,
        display_newest=True,
        enabled=True,
    )
    item = AlbumItem("photo-one", "https://cdn.icloud-content.com/synthetic.jpg", 1, "version-one")
    payload = jpeg(photo())
    thumb = jpeg(photo().resize((240, 180)), 85)
    monkeypatch.setattr(
        live_album, "fetch_album_items", Mock(return_value=AlbumSnapshot("Family", 1, (item,)))
    )
    monkeypatch.setattr(live_album, "download_album_image", Mock(return_value=payload))
    monkeypatch.setattr(live_album.IntegrationPublisher, "publish", Mock(return_value=[]))
    monkeypatch.setattr(
        controller,
        "get_status",
        Mock(
            return_value=SimpleNamespace(reachable=True, art_mode_supported=True, art_mode_on=True)
        ),
    )
    monkeypatch.setattr(
        controller,
        "upload_image",
        Mock(return_value=SimpleNamespace(success=True, content_id="MY-FAMILY", deferred=False)),
    )
    monkeypatch.setattr(controller, "switch_art", Mock(return_value=True))
    monkeypatch.setattr(controller, "delete_art", Mock(return_value=True))
    single = Mock(side_effect=AssertionError("Album verification must use batch transport"))
    monkeypatch.setattr(controller, "get_art_thumbnail", single)
    batch = Mock(return_value={})
    monkeypatch.setattr(controller, "get_art_thumbnails", batch)
    return SimpleNamespace(
        settings=settings,
        store=store,
        album=album,
        item=item,
        service=LiveAlbumService(lambda: settings),
        payload=payload,
        thumb=thumb,
        batch=batch,
        token=token,
        single=single,
    )


def saved(state):
    return state.store.current_items(state.album["id"], "family")[state.item.item_id]


def unverified(state):
    row = saved(state)
    return state.store.upload_is_unverified(
        state.album["id"], "family", state.item.item_id, state.item.checksum, row["content_id"]
    )


def test_delayed_thumbnail_recovers_and_stays_verified_across_restart_without_reupload(state):
    state.service.sync_album(state.album["id"])
    assert unverified(state)
    state.batch.return_value = {"MY-FAMILY": state.thumb}
    state.service = LiveAlbumService(lambda: state.settings)
    result = state.service.sync_album(state.album["id"])
    assert result["status"] == "unchanged" and result["errors"] == []
    assert not unverified(state)
    assert state.service.sync_album(state.album["id"])["errors"] == []
    controller.upload_image.assert_called_once()
    controller.switch_art.assert_called_once()
    controller.delete_art.assert_not_called()
    state.single.assert_not_called()


def test_legacy_five_column_receipt_is_migrated_and_requires_source_pixel_evidence(state):
    state.service.sync_album(state.album["id"])
    with sqlite3.connect(state.store.database_path) as db:
        db.execute("DROP TABLE photo_album_upload_epochs")
    state.store = LiveAlbumStore(state.settings.data_dir)
    with state.store._connect() as db:
        assert len(db.execute("PRAGMA table_info(photo_album_unverified_uploads)").fetchall()) == 5
    state.batch.return_value = {"MY-FAMILY": jpeg(ImageOps.mirror(photo()).resize((240, 180)))}
    assert state.service.sync_album(state.album["id"])["errors"]
    assert unverified(state)
    state.batch.return_value = {"MY-FAMILY": state.thumb}
    assert state.service.sync_album(state.album["id"])["errors"] == []
    assert not unverified(state)
    controller.upload_image.assert_called_once()
    controller.delete_art.assert_not_called()


@pytest.mark.parametrize("kind", ["timeout", "missing", "wrong", "corrupt", "pairing"])
def test_unavailable_or_wrong_evidence_preserves_receipt_and_never_reuploads(state, kind):
    state.service.sync_album(state.album["id"])
    if kind == "timeout":
        state.batch.side_effect = TimeoutError("synthetic")
    elif kind == "missing":
        state.batch.return_value = {}
    elif kind == "wrong":
        state.batch.return_value = {"MY-FAMILY": jpeg(ImageOps.mirror(photo()).resize((240, 180)))}
    elif kind == "corrupt":
        state.batch.return_value = {"MY-FAMILY": b"bad-image"}
    else:
        state.token.write_text("synthetic-new-pairing")
        state.batch.return_value = {"MY-FAMILY": state.thumb}
    assert state.service.sync_album(state.album["id"])["errors"]
    assert unverified(state)
    controller.upload_image.assert_called_once()
    controller.switch_art.assert_called_once()
    controller.delete_art.assert_not_called()


def test_transient_read_failure_after_verification_preserves_claim_and_recovers(state):
    state.batch.return_value = {"MY-FAMILY": state.thumb}
    state.service.sync_album(state.album["id"])
    assert not unverified(state)
    state.batch.side_effect = TimeoutError("synthetic")
    assert state.service.sync_album(state.album["id"])["errors"]
    assert saved(state)["content_id"] == "MY-FAMILY"
    state.batch.side_effect = None
    assert state.service.sync_album(state.album["id"])["errors"] == []
    controller.upload_image.assert_called_once()
    controller.delete_art.assert_not_called()


@pytest.mark.parametrize("mode", [False, None])
def test_busy_or_unknown_tv_has_no_thumbnail_or_mutation_calls(state, mode):
    state.service.sync_album(state.album["id"])
    state.batch.reset_mock()
    controller.get_status.return_value.art_mode_on = mode
    assert state.service.sync_album(state.album["id"])["errors"]
    state.batch.assert_not_called()
    controller.upload_image.assert_called_once()
    controller.switch_art.assert_called_once()
    controller.delete_art.assert_not_called()


@pytest.mark.parametrize("change", ["claim", "source", "pairing"])
def test_changed_claim_source_or_pairing_during_read_cannot_establish_baseline(state, change):
    state.service.sync_album(state.album["id"])
    row = saved(state)

    def fetched(*_args):
        if change == "claim":
            state.store.forget_content_claim(state.album["id"], "family", "MY-FAMILY")
        elif change == "source":
            with state.store._connect() as db:
                db.execute("UPDATE photo_album_source_items SET source_checksum='new-version'")
        else:
            state.token.write_text("synthetic-new-pairing")
        return {"MY-FAMILY": state.thumb}

    state.batch.side_effect = fetched
    assert not state.store.reconcile_upload(
        state.settings, "family", state.album["id"], row, state.payload
    )
    with state.store._connect() as db:
        assert db.execute("SELECT count(*) FROM catalog_tv_fingerprints").fetchone()[0] == 0
    controller.upload_image.assert_called_once()
    controller.delete_art.assert_not_called()


def test_manual_display_reconciles_without_upload_and_retains_art_mode_guard(state):
    state.service.sync_album(state.album["id"])
    state.batch.return_value = {"MY-FAMILY": state.thumb}
    result = state.service.display_photo(state.album["id"], state.item.item_id)
    assert not result["errors"]
    assert not unverified(state)
    controller.upload_image.assert_called_once()
    assert controller.switch_art.call_args.kwargs["require_art_mode"] is True
