"""Dislike persistence, full-frame replacement, recovery and calendar safety."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from PIL import Image
from pydantic import SecretStr

from frameart.art_catalog import ArtCatalogStore, ArtistMetadata, ArtworkMetadata
from frameart.art_delivery import CatalogDeliveryService, purge_cached_artwork
from frameart.config import Settings
from frameart.curated_replacements import (
    CuratedReplacementService,
    CuratedReplacementStore,
    _fingerprints,
    _queries,
    find_replacement,
)
from frameart.daily_art import DailyArtStore


def jpeg(color="blue", size=(3840, 2160)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG")
    return output.getvalue()


def artwork(identity="new-9000", title="New landscape", source_id=9000):
    return ArtworkMetadata(
        id=identity,
        title=title,
        artist=ArtistMetadata(id="artist", name="A real artist"),
        source_url=f"https://www.artic.edu/artworks/{source_id}",
        license="Public Domain (CC0)",
        width=3840,
        height=2160,
    )


@pytest.fixture
def collection(tmp_path):
    settings = Settings(data_dir=tmp_path)
    catalog = ArtCatalogStore(tmp_path)
    records = [(artwork(f"art-{i}", f"Artwork {i}", 1000 + i), b"preview") for i in range(365)]
    catalog.replace(records, settings)
    calendar = DailyArtStore(tmp_path)
    calendar.set_calendar(
        {
            "year": 2027,
            "entries": [
                {
                    "date": (date(2027, 1, 1) + timedelta(days=i)).isoformat(),
                    "artwork_id": f"art-{i}",
                }
                for i in range(365)
            ],
        }
    )
    return settings, catalog, records, calendar


def factory(*_args, **_kwargs):
    return artwork(), jpeg(size=(1400, 800)), jpeg()


def test_dislike_deletes_active_pixels_and_refresh_cannot_resurrect(collection):
    settings, catalog, records, _ = collection
    store = CuratedReplacementStore(settings.data_dir)
    assert store.dislike("art-0")["status"] == "queued"
    assert catalog.get("art-0") is None
    assert catalog.thumbnail("art-0") is None
    assert catalog.snapshot(settings)["item_count"] == 364
    catalog.replace(records, settings)
    assert ArtCatalogStore(settings.data_dir).get("art-0") is None
    assert catalog.snapshot(settings)["item_count"] == 364
    assert store.summary()["rejected_count"] == 1


def test_double_click_and_restart_create_only_one_replacement(collection):
    settings, catalog, _, calendar = collection
    store = CuratedReplacementStore(settings.data_dir)
    first = store.dislike("art-0")
    assert store.dislike("art-0") == first
    service = CuratedReplacementService(lambda: settings, finder=factory)
    with patch("frameart.tv.controller.get_status", side_effect=AssertionError("No TV calls")):
        assert service.run_once()
    assert store.summary()["pending_count"] == 0
    assert catalog.snapshot(settings)["item_count"] == 365
    assert catalog.get("new-9000").title == "New landscape"
    assert not CuratedReplacementService(lambda: settings, finder=factory).run_once()
    with calendar._connect() as db:
        assert db.execute("SELECT count(*) FROM daily_art_calendar").fetchone()[0] == 365
        assert (
            db.execute(
                "SELECT artwork_id FROM daily_art_calendar WHERE day='2027-01-01'"
            ).fetchone()[0]
            == "new-9000"
        )
    assert store.dislike("art-0")["status"] == "completed"


def test_replacement_is_full_frame_sendable_without_resourcespace(collection):
    settings, catalog, _, _ = collection
    service = CuratedReplacementService(lambda: settings, finder=factory)
    service.dislike("art-0")
    service.run_once()
    with patch(
        "frameart.art_delivery.ResourceSpaceClient", side_effect=AssertionError("No network")
    ):
        payload, digest = CatalogDeliveryService(lambda: settings)._rendition(
            settings, catalog.get("new-9000")
        )
    assert hashlib.sha256(payload).hexdigest() == digest
    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == (3840, 2160)
        assert image.format == "JPEG" and image.mode == "RGB"


def test_unavailable_museum_retries_without_restoring_rejected_piece(collection):
    settings, catalog, _, _ = collection
    service = CuratedReplacementService(
        lambda: settings,
        finder=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret upstream error")
        ),
    )
    service.dislike("art-0")
    assert service.run_once()
    state = CuratedReplacementStore(settings.data_dir).summary()
    assert state["jobs"][0]["status"] == "retrying"
    assert "secret" not in json.dumps(state)
    assert not service.run_once()
    assert catalog.get("art-0") is None
    CuratedReplacementStore(settings.data_dir).retry("art-0")
    service.finder = factory
    assert service.run_once()
    assert catalog.snapshot(settings)["item_count"] == 365


def test_prepared_asset_survives_calendar_failure_without_redownload(collection):
    settings, catalog, _, _ = collection
    service = CuratedReplacementService(lambda: settings, finder=factory)
    service.dislike("art-0")
    with patch.object(DailyArtStore, "replace_unattempted_artwork", side_effect=OSError("busy")):
        service.run_once()
    assert catalog.get("new-9000") is None  # not exposed until calendar reconciliation finishes
    store = CuratedReplacementStore(settings.data_dir)
    store.retry("art-0")
    service = CuratedReplacementService(
        lambda: settings,
        finder=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Must reuse snapshot")),
    )
    service.run_once()
    assert catalog.get("new-9000") is not None
    assert store.summary()["pending_count"] == 0


@pytest.mark.parametrize("kind", ["source", "rendition", "identity", "title"])
def test_duplicate_or_previously_rejected_art_is_never_a_new_replacement(collection, kind):
    settings, catalog, _, _ = collection
    service = CuratedReplacementService(lambda: settings, finder=factory)
    service.dislike("art-0")
    service.run_once()
    first_source, first_rendition = factory()[1:]
    service.dislike("new-9000")
    new = artwork("other-new", "Different artwork", 9001)
    source, rendition = jpeg("red", (1400, 800)), jpeg("red")
    if kind == "source":
        source = first_source
    elif kind == "rendition":
        rendition = first_rendition
    elif kind == "identity":
        new.source_url = "https://www.artic.edu/artworks/9000/a-different-slug"
    else:
        new.title = "New landscape"
    service.finder = lambda *_a, **_k: (new, source, rendition)
    service.run_once()
    assert catalog.get("other-new") is None
    assert catalog.get("new-9000") is None
    assert CuratedReplacementStore(settings.data_dir).summary()["pending_count"] == 1


@pytest.mark.parametrize("shape", [(1920, 1080), (3840, 2161), (2160, 3840)])
def test_non_full_frame_derivatives_fail_closed(collection, shape):
    settings, catalog, _, _ = collection
    service = CuratedReplacementService(
        lambda: settings,
        finder=lambda *_a, **_k: (artwork(), jpeg(size=(1400, 800)), jpeg(size=shape)),
    )
    service.dislike("art-0")
    service.run_once()
    assert catalog.get("new-9000") is None
    assert CuratedReplacementStore(settings.data_dir).summary()["pending_count"] == 1


def test_replacement_id_cannot_collide_with_an_existing_different_artwork(collection):
    settings, catalog, _, _ = collection
    service = CuratedReplacementService(
        lambda: settings,
        finder=lambda *_a, **_k: (artwork("art-1"), jpeg(size=(1400, 800)), jpeg()),
    )
    service.dislike("art-0")
    service.run_once()
    assert catalog.get("art-1").title == "Artwork 1"
    assert catalog.snapshot(settings)["item_count"] == 364
    assert CuratedReplacementStore(settings.data_dir).summary()["pending_count"] == 1


def test_only_one_worker_claims_and_expired_claim_cannot_publish(collection, monkeypatch):
    settings, _, _, _ = collection
    store = CuratedReplacementStore(settings.data_dir)
    store.dislike("art-0")
    store.dislike("art-1")
    first = store.claim()
    assert CuratedReplacementStore(settings.data_dir).claim() is None
    with store._db() as db:
        db.execute("UPDATE curated_replacement_jobs SET lease_until=0 WHERE artwork_id='art-0'")
    second = CuratedReplacementStore(settings.data_dir).claim()
    assert second["claim_token"] != first["claim_token"]
    with pytest.raises(ValueError, match="claim"):
        store.prepare(first, *factory())


def test_attempted_calendar_day_and_dispatch_history_are_not_rewritten(collection):
    settings, _, _, calendar = collection
    calendar.configure(
        {"enabled": True, "target_id": "family", "change_time": "00:00", "timezone": "UTC"}
    )
    claim = calendar._claim(datetime(2027, 1, 1, tzinfo=timezone.utc), 3600)
    assert claim["artwork_id"] == "art-0"
    with calendar._connect() as db:
        original_run = tuple(db.execute("SELECT * FROM daily_art_runs").fetchone())
    service = CuratedReplacementService(lambda: settings, finder=factory)
    service.dislike("art-0")
    service.run_once()
    with calendar._connect() as db:
        assert (
            db.execute(
                "SELECT artwork_id FROM daily_art_calendar WHERE day='2027-01-01'"
            ).fetchone()[0]
            == "art-0"
        )
        assert tuple(db.execute("SELECT * FROM daily_art_runs").fetchone()) == original_run
    assert calendar.config()["enabled"]


def test_pending_and_completed_replacements_survive_upstream_refresh(collection):
    settings, catalog, records, _ = collection
    service = CuratedReplacementService(lambda: settings, finder=factory)
    service.dislike("art-0")
    service.run_once()
    catalog.replace(records, settings)
    assert catalog.snapshot(settings)["item_count"] == 365
    assert catalog.get("art-0") is None
    assert catalog.get("new-9000") is not None


def test_local_corruption_is_not_sent(collection):
    settings, catalog, _, _ = collection
    service = CuratedReplacementService(lambda: settings, finder=factory)
    service.dislike("art-0")
    service.run_once()
    with CuratedReplacementStore(settings.data_dir)._db() as db:
        db.execute("UPDATE curated_local_art SET rendition=?", (b"corrupted",))
    with pytest.raises(Exception, match="integrity"):
        catalog.local_rendition("new-9000")


def test_artwork_rejected_during_download_is_not_reinserted_in_cache(collection):
    settings, catalog, _, _ = collection
    settings = settings.model_copy(
        update={
            "resourcespace_url": "https://catalog.example.test",
            "resourcespace_user": "reader",
            "resourcespace_api_key": SecretStr("synthetic-key"),
            "resourcespace_collection": 1,
        }
    )
    selected = catalog.get("art-0")
    selected.rendition_ref = 10
    store = CuratedReplacementStore(settings.data_dir)

    def download(_ref):
        store.dislike("art-0")
        return jpeg()

    from frameart.art_delivery import CatalogDeliveryStore
    from frameart.resourcespace import ResourceSpaceError

    CatalogDeliveryStore(settings.data_dir)
    with patch("frameart.art_delivery.ResourceSpaceClient") as client:
        client.return_value.rendition.side_effect = download
        with pytest.raises(ResourceSpaceError, match="removed"):
            CatalogDeliveryService(lambda: settings)._rendition(settings, selected)
    assert not list((settings.data_dir / "catalog-renditions").glob("*.jpg"))


def test_unknown_and_unsafe_ids_do_not_delete_anything(collection):
    settings, catalog, _, _ = collection
    store = CuratedReplacementStore(settings.data_dir)
    with pytest.raises(ValueError):
        store.dislike("../../artwork")
    with pytest.raises(KeyError):
        store.dislike("not-in-catalog")
    assert catalog.snapshot(settings)["item_count"] == 365


def test_purge_only_removes_unshared_local_cache(collection):
    settings, _, _, _ = collection
    from frameart.art_delivery import CatalogDeliveryStore, _database

    CatalogDeliveryStore(settings.data_dir)
    directory = settings.data_dir / "catalog-renditions"
    directory.mkdir()
    digest = hashlib.sha256(b"cached").hexdigest()
    path = directory / (digest + ".jpg")
    path.write_bytes(b"cached")
    with _database(settings.data_dir) as db:
        db.executemany(
            "INSERT INTO catalog_renditions VALUES(?,?,?,?)",
            [("art-0", 1, digest, 0), ("art-1", 2, digest, 0)],
        )
    purge_cached_artwork(settings.data_dir, "art-0")
    assert path.exists()
    purge_cached_artwork(settings.data_dir, "art-1")
    assert not path.exists()


def test_queries_use_genre_preferences_not_household_free_text():
    profile = {
        "derived": {"preferred_tags": [{"tag": "abstract"}, {"tag": "bold"}]},
        "household": {"notes": "private family details"},
    }
    old = artwork()
    old.tags = ["season:winter"]
    queries = _queries(profile, old)
    assert queries[0] == "winter landscape"
    assert "abstract painting" in queries
    assert "private" not in json.dumps(queries)


def test_fingerprint_recognizes_source_id_despite_slug_changes():
    first = artwork()
    second = artwork("different-id", "Different title")
    second.source_url += "/slug"
    assert "aic:9000" in _fingerprints(first).intersection(_fingerprints(second))


def test_dislike_routes_require_admin_scope():
    from fastapi import Request

    from frameart.api import _required_scope

    for path, method in (
        ("/curated-artwork/art-0/dislike", "POST"),
        ("/curated-artwork/art-0/retry", "POST"),
    ):
        request = Request({"type": "http", "path": path, "method": method, "headers": []})
        assert _required_scope(request) == "admin"


def test_router_dislike_is_idempotent_and_missing_artwork_is_404(collection):
    from fastapi import HTTPException, Response

    from frameart.art_catalog_api import create_art_catalog_router

    settings, _, _, _ = collection
    service = CuratedReplacementService(lambda: settings, finder=factory)
    router = create_art_catalog_router(lambda: settings, replacements=service)
    action = next(route.endpoint for route in router.routes if route.path.endswith("/dislike"))
    assert action("art-0", Response())["status"] == "queued"
    assert action("art-0", Response())["status"] == "queued"
    with pytest.raises(HTTPException) as caught:
        action("unknown", Response())
    assert caught.value.status_code == 404


def test_finder_rechecks_public_domain_and_builds_full_frame(collection, monkeypatch):
    settings, _, _, _ = collection
    source = jpeg(size=(1400, 800))
    monkeypatch.setattr("frameart.curated_replacements.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "frameart.curated_replacements.public_domain.search_artworks",
        lambda *_a, **_k: [{"artwork_id": "9000"}],
    )
    metadata = {
        "is_public_domain": True,
        "title": "New landscape",
        "artist": "Artist",
        "source_url": "https://www.artic.edu/artworks/9000",
        "image_url": "https://www.artic.edu/iiif/2/abc-123/full/1686,/0/default.jpg",
    }
    monkeypatch.setattr(
        "frameart.curated_replacements.public_domain.get_artwork", lambda *_a, **_k: metadata
    )
    import httpx2 as httpx

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=source))
    real_client = httpx.Client
    monkeypatch.setattr(
        "frameart.curated_replacements.httpx.Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    record, original, rendition = find_replacement(settings, artwork("old", "Old", 2), set())
    assert original == source
    assert record.source_url.endswith("/9000")
    with Image.open(io.BytesIO(rendition)) as image:
        assert image.size == (3840, 2160)
    metadata["is_public_domain"] = False
    with pytest.raises(ValueError):
        find_replacement(settings, artwork(), set())
