"""Offline-first metadata, cache transactions and API contract tests."""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr, ValidationError

from frameart import art_catalog
from frameart.art_catalog import (
    ArtCatalogStore,
    ArtworkMetadata,
    CatalogSyncBusyError,
    artwork_from_fields,
    sync_catalog,
)
from frameart.art_catalog_api import create_art_catalog_router
from frameart.config import Settings
from frameart.resourcespace import ResourceSpaceClient, ResourceSpaceError


def artwork_data(**updates):
    record = {
        "schema_version": 1,
        "id": "water-lilies",
        "title": "Water Lilies",
        "artist": {
            "id": "wikidata-Q296",
            "name": "Claude Monet",
            "birth_year": 1840,
            "death_year": 1926,
            "source_url": "https://www.wikidata.org/wiki/Q296",
        },
        "date": "1906",
        "medium": "Oil on canvas",
        "license": "Public Domain (CC0)",
        "source_url": "https://www.artic.edu/artworks/16568",
        "width": 3840,
        "height": 2160,
        "tags": ["water", "traditional"],
        "ai_generated": False,
    }
    return record | updates


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEART_DATA_DIR", str(tmp_path))
    return Settings(
        data_dir=tmp_path,
        resourcespace_url="http://catalog.local",
        resourcespace_user="reader",
        resourcespace_api_key="private-reader-key",
        resourcespace_collection=9,
    )


def thumbnail_bytes():
    destination = io.BytesIO()
    Image.new("RGB", (1200, 675), "blue").save(destination, "JPEG")
    return destination.getvalue()


class FakeResourceSpace:
    resources = [{"ref": "7", "preview_extension": "jpg"}]
    fields = {"frameart_metadata": json.dumps(artwork_data())}

    def __init__(self, *args, **kwargs):
        pass

    def collection_resources(self, collection_id):
        assert collection_id == 9
        return self.resources

    def resource_fields(self, ref):
        assert ref == 7
        return self.fields

    def thumbnail(self, ref, extension):
        assert ref == 7
        return thumbnail_bytes()


def test_metadata_preserves_artist_and_artwork_distinction():
    data = artwork_data(source_publication_date="2026-03-01")
    artwork = artwork_from_fields(7, {"frameart_metadata": json.dumps(data)})
    assert artwork.artist.birth_year == 1840
    assert artwork.date == "1906"
    assert artwork.source_publication_date == "2026-03-01"
    assert artwork.rendition_ref == 7
    assert not artwork.ai_generated


def test_real_resourcespace_sanitized_fields_keep_artist_and_canonical_metadata(monkeypatch):
    client = ResourceSpaceClient("http://catalog.local", "reader", "key")
    monkeypatch.setattr(
        client,
        "call",
        lambda *args, **kwargs: [
            {"name": "frameartmetadata", "value": json.dumps(artwork_data())},
            {"name": "frameartartist", "value": "Claude Monet"},
            {"name": "frameartartistbio", "value": "Verified artist biography."},
            {"name": "frameartdate", "value": "1906"},
            {"name": "frameartmedium", "value": "Oil on canvas"},
            {"name": "frameartlicense", "value": "Public Domain (CC0)"},
            {"name": "frameartsourceurl", "value": "https://www.artic.edu/artworks/16568"},
        ],
    )
    record = artwork_from_fields(7, client.resource_fields(7))
    assert record.id == "water-lilies"
    assert record.artist.id == "wikidata-Q296"
    assert record.artist.name == "Claude Monet"
    assert record.artist.bio == "Verified artist biography."
    assert record.date == "1906"
    assert record.license == "Public Domain (CC0)"


def test_readable_resourcespace_edits_override_canonical_json():
    artwork = artwork_from_fields(
        7,
        {
            "frameart_metadata": json.dumps(artwork_data()),
            "title": "Corrected title",
            "frameart_artist": "Corrected artist",
            "frameart_artist_bio": "Verified biography.",
            "frameart_medium": "Corrected medium",
            "frameart_license": "CC0",
            "frameart_date": "circa 1906",
            "frameart_source_url": "https://museum.example/item",
        },
    )
    assert artwork.title == "Corrected title"
    assert artwork.artist.name == "Corrected artist"
    assert artwork.artist.bio == "Verified biography."
    assert artwork.medium == "Corrected medium"
    assert artwork.license == "CC0"
    assert artwork.date == "circa 1906"
    assert artwork.source_url == "https://museum.example/item"


def test_verified_museum_and_crop_metadata_passes_through_with_limits():
    values = {
        "institution": "Art Institute of Chicago",
        "accession_number": "1933.1157",
        "work_dimensions": "Source museum dimensions retained without conversion.",
        "crop_notes": "Approved 16:9 crop; no padding, matte or stretching.",
    }
    record = artwork_from_fields(7, {"frameart_metadata": json.dumps(artwork_data(**values))})
    for field, expected in values.items():
        assert record.model_dump()[field] == expected
    with pytest.raises(ValidationError):
        ArtworkMetadata.model_validate(artwork_data(institution="x" * 301))
    with pytest.raises(ValidationError):
        ArtworkMetadata.model_validate(artwork_data(crop_notes="x" * 3001))


@pytest.mark.parametrize("data", ["not json", "[]", '{"schema_version":2}', '{"artist":"wrong"}'])
def test_malformed_metadata_fails_safely(data):
    with pytest.raises(ResourceSpaceError, match="invalid FrameArt"):
        artwork_from_fields(7, {"frameart_metadata": data})


def test_unknown_history_is_not_invented():
    artwork = artwork_from_fields(7, {"title": "Uncatalogued painting"})
    assert artwork.artist.name == "Unknown artist"
    assert artwork.artist.bio is None
    assert artwork.date is None
    assert artwork.medium is None


def test_metadata_rejects_script_links_and_path_ids():
    with pytest.raises(ValidationError):
        ArtworkMetadata.model_validate(artwork_data(source_url="javascript:alert(1)"))
    with pytest.raises(ValidationError):
        ArtworkMetadata.model_validate(artwork_data(id="../private"))


def test_sync_caches_metadata_and_bounded_preview(settings, monkeypatch):
    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", FakeResourceSpace)
    result = sync_catalog(settings)
    assert result["status"] == "ready"
    assert result["item_count"] == 1
    assert result["last_synced_at"]
    assert result["items"][0]["thumbnail_url"] == "/art-catalog/water-lilies/thumbnail"
    assert "private-reader-key" not in json.dumps(result)
    cached = ArtCatalogStore(settings.data_dir).thumbnail("water-lilies")
    with Image.open(io.BytesIO(cached)) as image:
        assert image.format == "WEBP"
        assert image.size == (800, 450)


def test_empty_sync_retains_previous_metadata_and_preview(settings, monkeypatch):
    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", FakeResourceSpace)
    sync_catalog(settings)
    previous = ArtCatalogStore(settings.data_dir).thumbnail("water-lilies")
    monkeypatch.setattr(FakeResourceSpace, "resources", [])
    with pytest.raises(ResourceSpaceError, match="previous catalog was retained"):
        sync_catalog(settings)
    snapshot = ArtCatalogStore(settings.data_dir).snapshot(settings)
    assert snapshot["status"] == "stale"
    assert snapshot["item_count"] == 1
    assert ArtCatalogStore(settings.data_dir).thumbnail("water-lilies") == previous


def test_mid_sync_failure_does_not_erase_previous_snapshot(settings, monkeypatch):
    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", FakeResourceSpace)
    original = sync_catalog(settings)

    def fail(*args):
        raise RuntimeError("sensitive private-reader-key upstream URL")

    monkeypatch.setattr(FakeResourceSpace, "resource_fields", fail)
    with pytest.raises(ResourceSpaceError) as error:
        sync_catalog(settings)
    assert "private-reader-key" not in str(error.value)
    snapshot = ArtCatalogStore(settings.data_dir).snapshot(settings)
    assert snapshot["items"] == original["items"]
    assert snapshot["last_synced_at"] == original["last_synced_at"]
    assert snapshot["status"] == "stale"
    assert "private-reader-key" not in json.dumps(snapshot)


def test_duplicate_ids_do_not_replace_existing_cache(settings):
    store = ArtCatalogStore(settings.data_dir)
    record = ArtworkMetadata.model_validate(artwork_data())
    store.replace([(record, b"old-preview")], settings)
    with pytest.raises(ResourceSpaceError, match="duplicate"):
        store.replace([(record, b"new"), (record, None)], settings)
    assert store.thumbnail(record.id) == b"old-preview"


def test_cached_lookup_returns_typed_metadata_without_network(settings, monkeypatch):
    store = ArtCatalogStore(settings.data_dir)
    record = ArtworkMetadata.model_validate(artwork_data(rendition_ref=7))
    store.replace([(record, b"preview")], settings)

    def forbidden(*args, **kwargs):
        raise AssertionError("Cached artwork lookup must not contact ResourceSpace")

    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", forbidden)
    assert store.get("water-lilies") == record
    assert store.get("missing") is None
    assert store.get("' OR 1=1 --") is None


def test_full_year_sync_reports_progress_and_gets_all_cached_items(settings, monkeypatch):
    preview = thumbnail_bytes()

    class FullLibrary:
        def __init__(self, *args, **kwargs):
            pass

        def collection_resources(self, collection_id):
            return [{"ref": ref} for ref in range(1, 366)]

        def resource_fields(self, ref):
            return {"frameart_metadata": json.dumps(artwork_data(id=f"artwork-{ref}"))}

        def thumbnail(self, ref, extension):
            return preview

    # JPEG/WebP conversion is tested independently; avoid encoding 365 copies
    # here so this test focuses on complete catalog persistence and progress.
    monkeypatch.setattr(art_catalog, "_webp_thumbnail", lambda payload: b"cached-webp")
    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", FullLibrary)
    progress = []
    snapshot = sync_catalog(settings, lambda done, total: progress.append((done, total)))
    assert snapshot["status"] == "ready"
    assert snapshot["item_count"] == len(snapshot["items"]) == 365
    assert snapshot["catalog_limit"] == 1000
    assert progress == [(done, 365) for done in range(366)]
    store = ArtCatalogStore(settings.data_dir)
    assert store.get("artwork-365").rendition_ref == 365

    def forbidden(*args, **kwargs):
        raise AssertionError("Catalog GET must remain local, even for a full year")

    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", forbidden)
    app = FastAPI()
    app.include_router(create_art_catalog_router(lambda: settings))
    with TestClient(app) as client:
        response = client.get("/art-catalog")
        assert response.status_code == 200
        assert response.json()["items"] == snapshot["items"]


@pytest.mark.parametrize("failure", ["pagination", "metadata", "duplicate_ids", "overflow"])
def test_large_library_failure_preserves_exact_previous_cache(settings, monkeypatch, failure):
    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", FakeResourceSpace)
    baseline = sync_catalog(settings)
    store = ArtCatalogStore(settings.data_dir)
    old_thumbnail = store.thumbnail("water-lilies")

    class BrokenLibrary:
        def __init__(self, *args, **kwargs):
            pass

        def collection_resources(self, collection_id):
            if failure == "pagination":
                raise ResourceSpaceError("ResourceSpace returned an incomplete collection page.")
            return [{"ref": ref} for ref in range(1, 1002 if failure == "overflow" else 366)]

        def resource_fields(self, ref):
            if failure == "metadata" and ref == 250:
                raise RuntimeError("private-reader-key and private upstream URL")
            artwork_id = "duplicated" if failure == "duplicate_ids" else f"artwork-{ref}"
            return {"frameart_metadata": json.dumps(artwork_data(id=artwork_id))}

        def thumbnail(self, ref, extension):
            return b"not-needed"

    monkeypatch.setattr(art_catalog, "_webp_thumbnail", lambda payload: b"new-preview")
    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", BrokenLibrary)
    with pytest.raises(ResourceSpaceError) as error:
        sync_catalog(settings)
    assert "private-reader-key" not in str(error.value)
    retained = store.snapshot(settings)
    assert retained["status"] == "stale"
    assert retained["items"] == baseline["items"]
    assert retained["last_synced_at"] == baseline["last_synced_at"]
    assert store.thumbnail("water-lilies") == old_thumbnail


def test_catalog_store_accepts_1000_but_refuses_overflow_atomically(settings):
    store = ArtCatalogStore(settings.data_dir)
    records = [
        (ArtworkMetadata.model_validate(artwork_data(id=f"artwork-{ref}")), b"preview")
        for ref in range(1000)
    ]
    store.replace(records, settings)
    baseline = store.snapshot(settings)
    assert baseline["item_count"] == 1000
    with pytest.raises(ResourceSpaceError, match="size limit"):
        store.replace(records + [(ArtworkMetadata.model_validate(artwork_data()), None)], settings)
    assert store.snapshot(settings) == baseline


def test_concurrent_sync_is_rejected_and_lock_is_released(settings, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    failures = []

    class BlockingClient(FakeResourceSpace):
        def collection_resources(self, collection_id):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("Test synchronization timed out.")
            return super().collection_resources(collection_id)

    def worker():
        try:
            sync_catalog(settings)
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", BlockingClient)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(CatalogSyncBusyError):
            sync_catalog(settings)
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert failures == []
    assert sync_catalog(settings)["status"] == "ready"


def test_get_catalog_and_thumbnail_work_offline_and_do_not_contact_server(settings, monkeypatch):
    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", FakeResourceSpace)
    sync_catalog(settings)

    def forbidden(*args, **kwargs):
        raise AssertionError("Browsing must never perform a network call.")

    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", forbidden)
    app = FastAPI()
    app.include_router(create_art_catalog_router(lambda: settings))
    with TestClient(app) as client:
        response = client.get("/art-catalog")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["items"][0]["artist"]["name"] == "Claude Monet"
        preview = client.get(response.json()["items"][0]["thumbnail_url"])
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/webp"
        assert (
            client.get(
                "/art-catalog/water-lilies/thumbnail",
                headers={"If-None-Match": preview.headers["etag"]},
            ).status_code
            == 304
        )
        assert client.get("/art-catalog/missing/thumbnail").status_code == 404


def test_not_configured_api_never_attempts_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEART_DATA_DIR", str(tmp_path))
    settings = Settings(data_dir=tmp_path)
    app = FastAPI()
    app.include_router(create_art_catalog_router(lambda: settings))
    with TestClient(app) as client:
        assert client.get("/art-catalog").json()["status"] == "not_configured"
        assert client.post("/art-catalog/sync").status_code == 503


def test_ai_originals_keep_explicit_provenance():
    record = ArtworkMetadata.model_validate(
        artwork_data(
            id="ai-original",
            ai_generated=True,
            ai_provenance="Created with imagegen; prompt retained.",
            artist={"id": "ai-assisted", "name": "AI-assisted original"},
            date="2026",
        )
    )
    assert record.ai_generated
    assert record.ai_provenance
    assert record.artist.birth_year is None


def test_real_frameart_app_enforces_reader_and_admin_scopes_without_tv_calls(
    settings,
    monkeypatch,
):
    from frameart import api, catalog_sync
    from frameart.tv import controller

    secured = settings.model_copy(
        update={
            "auth_enabled": True,
            "admin_token": SecretStr("pilot-admin-token"),
            "automation_token": SecretStr("pilot-automation-token"),
            "trusted_lan_cidrs": [],
            "tailscale_auth_enabled": False,
            "api_rate_limit_per_minute": 10000,
        }
    )
    monkeypatch.setattr(api, "load_settings", lambda: secured)
    monkeypatch.setattr(art_catalog, "ResourceSpaceClient", FakeResourceSpace)

    def forbidden(*args, **kwargs):
        raise AssertionError("Artwork browsing and metadata sync must never contact a TV.")

    monkeypatch.setattr(api, "preflight_tv", forbidden)
    monkeypatch.setattr(controller, "_connect", forbidden)
    monkeypatch.setattr(controller, "_connect_art", forbidden)
    workers = []

    class ControlledThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            workers.append(self.target)

    monkeypatch.setattr(catalog_sync, "threading", SimpleNamespace(Thread=ControlledThread))
    # No lifespan context: this test exercises the actual middleware/router,
    # not unrelated scheduler startup or device discovery.
    client = TestClient(api.app)
    automation = {"Authorization": "Bearer pilot-automation-token"}
    admin = {"Authorization": "Bearer pilot-admin-token"}
    assert client.get("/art-catalog").status_code == 401
    assert client.post("/art-catalog/sync").status_code == 401
    assert client.get("/art-catalog", headers=automation).status_code == 200
    assert client.post("/art-catalog/sync", headers=automation).status_code == 403
    refreshed = client.post("/art-catalog/sync", headers=admin)
    assert refreshed.status_code == 202
    assert "private-reader-key" not in refreshed.text
    assert refreshed.json()["status"] == "running"
    assert len(workers) == 1
    workers[0]()  # Run deterministically, without sleeping or starting an external worker.
    completed = client.get("/art-catalog", headers=automation).json()
    assert completed["sync"]["status"] == "completed"
    preview_url = completed["items"][0]["thumbnail_url"]
    assert client.get(preview_url).status_code == 401
    assert client.get(preview_url, headers=automation).status_code == 200
    assert client.get("/art-catalog", headers=automation).json()["item_count"] == 1
