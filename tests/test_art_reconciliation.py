"""Evidence-based reconciliation cannot mutate a TV or create manual holds."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from frameart.art_catalog import ArtCatalogStore, ArtworkMetadata
from frameart.art_delivery import CatalogDeliveryService, CatalogDeliveryStore, tv_delivery_lock
from frameart.daily_art import DailyArtStore
from frameart.library import LibraryStore


@pytest.fixture
def reconciliation(tmp_path):
    """Use real local mappings/fingerprints; replace every remote read boundary."""
    with ExitStack() as stack:
        now = datetime(2027, 1, 1, 15, tzinfo=timezone.utc)
        stack.enter_context(patch("frameart.daily_art._now", return_value=now))
        stack.enter_context(patch("frameart.config._default_data_dir", return_value=tmp_path))
        token = tmp_path / "synthetic-pairing.token"
        token.write_text("synthetic-reconciliation-epoch")
        token.chmod(0o600)
        profile = SimpleNamespace(ip="192.0.2.90", token_file=str(token))
        settings = SimpleNamespace(
            data_dir=tmp_path,
            tvs={"family-room": profile},
            resourcespace_url="https://catalog.example.test",
            resourcespace_user="synthetic-reader",
            resourcespace_api_key=SimpleNamespace(get_secret_value=lambda: "test-key"),
            resourcespace_collection=1,
        )
        artwork = ArtworkMetadata(
            id="synthetic-reconcile-art", title="Synthetic artwork",
            artist={"id": "synthetic-artist", "name": "Synthetic artist"},
            rendition_ref=1, width=3840, height=2160,
        )
        ArtCatalogStore(tmp_path).replace([(artwork, None)], settings)
        CatalogDeliveryStore(tmp_path)
        daily = DailyArtStore(tmp_path)
        service = CatalogDeliveryService(lambda: settings)
        payload = b"synthetic-cached-rendition"
        digest = hashlib.sha256(payload).hexdigest()
        rendition = stack.enter_context(
            patch.object(service, "_rendition", return_value=(payload, digest))
        )
        upstream = stack.enter_context(patch("frameart.art_delivery.ResourceSpaceClient"))
        status = stack.enter_context(patch("frameart.tv.controller.get_status"))
        status.return_value = SimpleNamespace(
            reachable=True, art_mode_supported=True, art_mode_on=True,
        )
        current = stack.enter_context(
            patch("frameart.tv.controller._get_current_artwork", return_value="MY-KNOWN")
        )
        thumbnail = stack.enter_context(
            patch(
                "frameart.tv.controller.get_art_thumbnail", return_value=b"fresh-known-tv-thumbnail"
            )
        )
        with sqlite3.connect(tmp_path / "frameart.sqlite3") as db:
            db.execute(
                "INSERT INTO catalog_tv_content "
                "(tv_ip,rendition_hash,content_id,artwork_id) VALUES(?,?,?,?)",
                (profile.ip, digest, "MY-KNOWN", artwork.id),
            )
        assert service._record_fingerprint(settings, profile, "MY-KNOWN")
        thumbnail.reset_mock()
        forbidden = [stack.enter_context(patch("frameart.tv.controller." + name)) for name in (
            "upload_image", "switch_art", "delete_art", "_connect", "_connect_art",
        )]
        fixture = SimpleNamespace(
            service=service, settings=settings, profile=profile, artwork=artwork,
            status=status, current=current, thumbnail=thumbnail, rendition=rendition,
            upstream=upstream, daily=daily, digest=digest,
        )
        yield fixture
        for mutation in forbidden:
            mutation.assert_not_called()
        upstream.assert_not_called()
        assert daily.snapshot(now)["holds"] == []


def reconcile(fixture):
    return fixture.service.reconcile(
        fixture.artwork.id, "tv", "family-room", "2027-01-01", "family-room",
    )


def test_positive_current_artwork_with_fresh_matching_fingerprint_is_displayed(reconciliation):
    fixture = reconciliation

    assert reconcile(fixture) == "displayed"

    fixture.status.assert_called_once_with(fixture.profile)
    fixture.thumbnail.assert_called_once_with(fixture.profile, "MY-KNOWN")
    fixture.current.assert_called_once_with(fixture.profile, timeout_sec=5)
    history = LibraryStore(fixture.settings.data_dir).list_history()
    assert len(history) == 1
    assert history[0]["source"] == "daily-art"
    assert history[0]["content_id"] == "MY-KNOWN"
    assert history[0]["tv_target"] == "family-room"
    assert fixture.daily.snapshot()["holds"] == []


def test_confirmed_other_artwork_is_retryable_without_any_tv_mutation(reconciliation):
    fixture = reconciliation
    fixture.current.return_value = "MY-OTHER"

    assert reconcile(fixture) == "retryable"

    assert LibraryStore(fixture.settings.data_dir).list_history() == []


@pytest.mark.parametrize("state", [
    {"reachable": False, "art_mode_supported": True, "art_mode_on": False},
    {"reachable": True, "art_mode_supported": True, "art_mode_on": False},
    {"reachable": True, "art_mode_supported": False, "art_mode_on": False},
    {"reachable": True, "art_mode_supported": True, "art_mode_on": None},
])
def test_offline_watching_unsupported_or_unknown_status_stays_uncertain(reconciliation, state):
    fixture = reconciliation
    fixture.status.return_value = SimpleNamespace(**state)

    assert reconcile(fixture) == "uncertain"

    fixture.rendition.assert_not_called()
    fixture.current.assert_not_called()
    fixture.thumbnail.assert_not_called()


def test_busy_delivery_lock_returns_uncertain_without_contacting_tv(reconciliation):
    fixture = reconciliation
    lock = tv_delivery_lock(fixture.profile.ip)
    with lock:
        assert reconcile(fixture) == "uncertain"
    fixture.status.assert_not_called()


def test_status_read_exception_stays_uncertain_without_fetching_images(reconciliation):
    fixture = reconciliation
    fixture.status.side_effect = RuntimeError("status unavailable")

    assert reconcile(fixture) == "uncertain"

    fixture.rendition.assert_not_called()
    fixture.current.assert_not_called()


def test_missing_mapping_is_uncertain_without_probing_unknown_content(reconciliation):
    fixture = reconciliation
    with sqlite3.connect(fixture.settings.data_dir / "frameart.sqlite3") as db:
        db.execute("DELETE FROM catalog_tv_content")

    assert reconcile(fixture) == "uncertain"

    fixture.current.assert_not_called()
    fixture.thumbnail.assert_not_called()


def test_wrong_rendition_hash_cannot_be_used_as_display_evidence(reconciliation):
    fixture = reconciliation
    fixture.rendition.return_value = (
        b"different-rendition", hashlib.sha256(b"different").hexdigest(),
    )

    assert reconcile(fixture) == "uncertain"

    fixture.current.assert_not_called()


def test_changed_thumbnail_fingerprint_prevents_recycled_content_id_reconciliation(reconciliation):
    fixture = reconciliation
    fixture.thumbnail.return_value = b"replacement-family-photo"

    assert reconcile(fixture) == "uncertain"

    fixture.current.assert_not_called()
    assert LibraryStore(fixture.settings.data_dir).list_history() == []


def test_changed_pairing_epoch_cannot_reuse_old_content_mapping(reconciliation):
    fixture = reconciliation
    Path(fixture.profile.token_file).write_text("different-synthetic-pairing-epoch")

    assert reconcile(fixture) == "uncertain"

    fixture.current.assert_not_called()


@pytest.mark.parametrize("current", [None, "", " ", "unknown", "None", "null", "off"])
def test_unknown_current_artwork_does_not_authorize_retry(reconciliation, current):
    fixture = reconciliation
    fixture.current.return_value = current

    assert reconcile(fixture) == "uncertain"

    assert LibraryStore(fixture.settings.data_dir).list_history() == []


def test_current_artwork_read_exception_stays_uncertain(reconciliation):
    fixture = reconciliation
    fixture.current.side_effect = RuntimeError("current artwork unavailable")

    assert reconcile(fixture) == "uncertain"

    assert LibraryStore(fixture.settings.data_dir).list_history() == []


def test_thumbnail_read_exception_stays_uncertain(reconciliation):
    fixture = reconciliation
    fixture.thumbnail.side_effect = RuntimeError("thumbnail unavailable")

    assert reconcile(fixture) == "uncertain"

    fixture.current.assert_not_called()
