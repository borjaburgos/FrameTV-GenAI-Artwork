"""Independent catalog-delivery safety tests with no TV or network access."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from frameart.art_catalog import ArtCatalogStore, ArtworkMetadata
from frameart.art_delivery import CatalogDeliveryService, CatalogDeliveryStore, resolve_targets
from frameart.automation import AutomationStore
from frameart.daily_art import DailyArtStore
from frameart.library import LibraryStore


def jpeg(color=(15, 35, 55), size=(3840, 2160), mode="RGB", file_type="JPEG"):
    destination = io.BytesIO()
    Image.new(mode, size, color).save(destination, file_type)
    return destination.getvalue()


class TestCatalogDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full_frame = jpeg()
        cls.full_hash = hashlib.sha256(cls.full_frame).hexdigest()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="frameart-delivery-test-")
        self.addCleanup(self.temporary.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = datetime(2027, 1, 1, 15, tzinfo=timezone.utc)
        self.day_key = "2027-01-01"
        self.stack.enter_context(patch("frameart.daily_art._now", return_value=self.now))
        self.data_dir = Path(self.temporary.name)
        self.stack.enter_context(
            patch("frameart.config._default_data_dir", return_value=self.data_dir)
        )
        family_token = self.data_dir / "synthetic-family.token"
        bedroom_token = self.data_dir / "synthetic-bedroom.token"
        family_token.write_text("synthetic-family-pairing-token")
        bedroom_token.write_text("synthetic-bedroom-pairing-token")
        family_token.chmod(0o600)
        bedroom_token.chmod(0o600)
        self.family = SimpleNamespace(ip="192.0.2.10", token_file=str(family_token))
        self.bedroom = SimpleNamespace(ip="192.0.2.11", token_file=str(bedroom_token))
        self.settings = SimpleNamespace(
            data_dir=self.data_dir,
            tvs={"family-room": self.family, "bedroom": self.bedroom},
            resourcespace_url="https://catalog.example.test",
            resourcespace_user="synthetic-reader",
            resourcespace_api_key=SimpleNamespace(get_secret_value=lambda: "test-only-key"),
            resourcespace_collection=1,
            resourcespace_timeout=1,
        )
        self.artwork = ArtworkMetadata(
            id="synthetic-art",
            title="Synthetic landscape",
            artist={"id": "synthetic-artist", "name": "Test artist"},
            license="CC0",
            width=3840,
            height=2160,
            rendition_ref=1,
        )
        self.catalog = ArtCatalogStore(self.data_dir)
        self.catalog.replace([(self.artwork, None)], self.settings)
        self.service = CatalogDeliveryService(lambda: self.settings)
        self.client_type = self.stack.enter_context(
            patch("frameart.art_delivery.ResourceSpaceClient")
        )
        self.client = self.client_type.return_value
        self.client.rendition.return_value = self.full_frame
        self.status = self.stack.enter_context(patch("frameart.tv.controller.get_status"))
        self.status.return_value = SimpleNamespace(
            reachable=True,
            art_mode_supported=True,
            art_mode_on=True,
        )
        self.inventory = self.stack.enter_context(
            patch("frameart.tv.controller.list_art_deduplicated")
        )
        self.inventory.return_value = []
        self.upload = self.stack.enter_context(patch("frameart.tv.controller.upload_image"))
        self.upload.return_value = SimpleNamespace(
            success=True,
            content_id="MY-SYNTHETIC-1",
            deferred=False,
        )
        self.select = self.stack.enter_context(
            patch("frameart.tv.controller.switch_art", return_value=True)
        )
        self.delete = self.stack.enter_context(
            patch("frameart.tv.controller.delete_art", return_value=True)
        )
        self.thumbnail = self.stack.enter_context(patch("frameart.tv.controller.get_art_thumbnail"))
        self.thumbnail.side_effect = lambda _profile, identity: ("thumbnail:" + identity).encode()

    def enable_daily(self, target_type="tv", target_id="family-room"):
        store = DailyArtStore(self.data_dir)
        store.set_calendar(
            {
                "year": 2027,
                "timezone": "America/New_York",
                "entries": [{"date": self.day_key, "artwork_id": self.artwork.id}],
            }
        )
        store.configure(
            {
                "enabled": True,
                "target_type": target_type,
                "target_id": target_id,
                "change_time": "00:00",
            }
        )
        return store

    def manual(self, targets=None):
        return self.service._deliver_targets(
            self.artwork.id,
            targets or ["family-room"],
            manual=True,
        )

    def map_content(self, content_id="MY-OLD"):
        CatalogDeliveryStore(self.data_dir)
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            connection.execute(
                "INSERT INTO catalog_tv_content "
                "(tv_ip,rendition_hash,content_id,artwork_id) VALUES(?,?,?,?)",
                (self.family.ip, self.full_hash, content_id, self.artwork.id),
            )
        self.assertTrue(self.service._record_fingerprint(self.settings, self.family, content_id))

    def seed_owned(self, count=9):
        CatalogDeliveryStore(self.data_dir)
        identities = [f"MY-DAILY-{index}" for index in range(count)]
        for identity in identities:
            self.assertTrue(self.service._record_fingerprint(self.settings, self.family, identity))
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            connection.executemany(
                "INSERT INTO catalog_daily_owned(tv_ip,content_id,profile_id,used_at) "
                "VALUES(?,?,?,?)",
                [
                    (self.family.ip, identity, "family-room", index)
                    for index, identity in enumerate(identities)
                ],
            )
        self.inventory.return_value = [{"content_id": identity} for identity in identities]
        return identities

    def test_busy_offline_unknown_and_unsupported_tvs_do_not_fetch_or_mutate(self):
        cases = (
            SimpleNamespace(reachable=True, art_mode_supported=True, art_mode_on=False),
            SimpleNamespace(reachable=False, art_mode_supported=True, art_mode_on=False),
            SimpleNamespace(reachable=True, art_mode_supported=False, art_mode_on=False),
            SimpleNamespace(reachable=True, art_mode_supported=True, art_mode_on=None),
        )
        for status in cases:
            with self.subTest(status=status):
                self.status.return_value = status
                result = self.manual()
                self.assertEqual(result[0]["status"], "deferred")
        self.client_type.assert_not_called()
        self.inventory.assert_not_called()
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_status_read_failure_does_not_fetch_upload_or_select(self):
        self.status.side_effect = RuntimeError(
            "https://catalog.example.test/image?access_key=private-test-value"
        )
        result = self.manual()
        self.assertEqual(result[0]["status"], "error")
        self.assertNotIn("private-test-value", str(result))
        self.client_type.assert_not_called()
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_group_only_delivers_to_confirmed_art_mode_tv(self):
        self.status.side_effect = lambda profile: SimpleNamespace(
            reachable=True,
            art_mode_supported=True,
            art_mode_on=profile is self.bedroom,
        )
        group = AutomationStore(self.data_dir).create_group(
            "Synthetic group",
            ["family-room", "bedroom"],
        )
        self.enable_daily("group", group["id"])
        result = self.service.deliver(
            self.artwork.id,
            "group",
            group["id"],
            self.day_key,
            skip_tvs=set(),
        )
        statuses = {row["tv"]: row["status"] for row in result["results"]}
        self.assertEqual(statuses, {"family-room": "deferred", "bedroom": "displayed"})
        self.assertIs(self.upload.call_args.args[0], self.bedroom)
        self.select.assert_called_once()

    def test_stale_reset_content_id_is_replaced_by_one_guarded_full_frame_upload(self):
        self.map_content()
        result = self.manual()
        self.assertEqual(result[0]["status"], "displayed")
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.args[1], self.full_frame)
        self.assertEqual(
            self.upload.call_args.kwargs,
            {
                "file_type": "JPEG",
                "matte": "none",
                "require_art_mode": True,
            },
        )
        self.select.assert_called_once_with(
            self.family,
            "MY-SYNTHETIC-1",
            require_art_mode=True,
        )
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            mapped = connection.execute(
                "SELECT content_id FROM catalog_tv_content WHERE tv_ip=? AND rendition_hash=?",
                (self.family.ip, self.full_hash),
            ).fetchone()
        self.assertEqual(mapped[0], "MY-SYNTHETIC-1")

    def test_live_mapped_inventory_is_reused_without_upload(self):
        self.map_content("MY-EXISTS")
        self.inventory.return_value = [{"content_id": "MY-EXISTS"}]
        result = self.manual()
        self.assertEqual(result[0]["status"], "displayed")
        self.upload.assert_not_called()
        self.select.assert_called_once_with(self.family, "MY-EXISTS", require_art_mode=True)

    def test_recycled_content_id_with_different_thumbnail_is_not_reused(self):
        self.map_content("MY-RECYCLED")
        self.inventory.return_value = [{"content_id": "MY-RECYCLED"}]
        self.thumbnail.side_effect = lambda _profile, identity: (
            b"different-family-photo"
            if identity == "MY-RECYCLED"
            else ("thumbnail:" + identity).encode()
        )
        result = self.manual()
        self.assertEqual(result[0]["status"], "displayed")
        self.upload.assert_called_once()
        self.select.assert_called_once_with(
            self.family,
            "MY-SYNTHETIC-1",
            require_art_mode=True,
        )
        self.delete.assert_not_called()

    def test_new_pairing_epoch_invalidates_reuse_and_old_ownership(self):
        identities = self.seed_owned()
        self.map_content(identities[0])
        Path(self.family.token_file).write_text("new-synthetic-pairing-epoch")
        result = self.manual()
        self.assertEqual(result[0]["status"], "displayed")
        self.upload.assert_called_once()
        self.select.assert_called_once_with(
            self.family,
            "MY-SYNTHETIC-1",
            require_art_mode=True,
        )
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            owners = connection.execute(
                "SELECT COUNT(*) FROM catalog_daily_owned WHERE tv_ip=?", (self.family.ip,)
            ).fetchone()[0]
        self.assertEqual(owners, 0)
        self.delete.assert_not_called()

    def test_post_upload_missing_thumbnail_does_not_invalidate_confirmed_display(self):
        self.enable_daily()
        self.thumbnail.side_effect = None
        self.thumbnail.return_value = None
        result = self.service.deliver(
            self.artwork.id,
            "tv",
            "family-room",
            self.day_key,
            skip_tvs=set(),
        )
        self.assertEqual(result["results"][0]["status"], "displayed")
        self.upload.assert_called_once()
        self.select.assert_called_once()
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM catalog_tv_fingerprints").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM catalog_daily_owned").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM catalog_tv_content").fetchone()[0], 0
            )

    def test_fingerprint_never_reads_thumbnails_with_unsafe_or_unbounded_token_file(self):
        symlink = self.data_dir / "token-symlink"
        symlink.symlink_to(self.family.token_file)
        oversized = self.data_dir / "oversized-token"
        oversized.write_bytes(b"x" * 4097)
        empty = self.data_dir / "empty-token"
        empty.write_bytes(b"")
        fifo = self.data_dir / "token-fifo"
        os.mkfifo(fifo)
        for path in (
            None,
            symlink,
            oversized,
            empty,
            fifo,
            self.data_dir,
            self.data_dir / "missing-token",
        ):
            with self.subTest(kind=str(path)):
                self.family.token_file = str(path) if path is not None else None
                self.assertIsNone(self.service._fingerprint(self.family, "MY-UNKNOWN"))
        self.thumbnail.assert_not_called()

    def test_pairing_change_during_thumbnail_read_cannot_create_a_fingerprint(self):
        def thumbnail(_profile, _identity):
            Path(self.family.token_file).write_text("changed-during-read")
            return b"thumbnail"

        self.thumbnail.side_effect = thumbnail
        self.assertIsNone(self.service._fingerprint(self.family, "MY-UNKNOWN"))

    def test_omitted_token_path_fingerprints_only_existing_canonical_controller_file(self):
        CatalogDeliveryStore(self.data_dir)
        self.family.token_file = None
        directory = self.data_dir / "secrets"
        directory.mkdir(mode=0o700)
        canonical = directory / "192_0_2_10.token"
        canonical.write_text("synthetic-canonical-pairing-token")
        canonical.chmod(0o600)
        previous = canonical.read_bytes()
        with patch("frameart.tv.controller._ensure_token_dir") as create_directory:
            self.assertTrue(
                self.service._record_fingerprint(self.settings, self.family, "MY-KNOWN")
            )
        create_directory.assert_not_called()
        self.assertEqual(canonical.read_bytes(), previous)
        self.thumbnail.assert_called_once_with(self.family, "MY-KNOWN")

    def test_omitted_token_path_does_not_create_a_missing_secrets_directory(self):
        self.family.token_file = None
        directory = self.data_dir / "secrets"
        self.assertFalse(directory.exists())
        self.assertIsNone(self.service._fingerprint(self.family, "MY-UNKNOWN"))
        self.assertFalse(directory.exists())
        self.thumbnail.assert_not_called()

    def test_omitted_token_path_rejects_a_symlink_at_the_canonical_filename(self):
        self.family.token_file = None
        directory = self.data_dir / "secrets"
        directory.mkdir(mode=0o700)
        canonical = directory / "192_0_2_10.token"
        canonical.symlink_to(self.data_dir / "synthetic-family.token")
        self.assertIsNone(self.service._fingerprint(self.family, "MY-UNKNOWN"))
        self.thumbnail.assert_not_called()

    def test_cached_bytes_avoid_second_resourcespace_fetch(self):
        self.manual()
        self.inventory.return_value = [{"content_id": "MY-SYNTHETIC-1"}]
        self.manual()
        self.client.rendition.assert_called_once_with(1)
        self.upload.assert_called_once()
        self.assertEqual(self.select.call_count, 2)

    def test_corrupt_cached_file_is_repaired_from_valid_source_without_duplicate_upload(self):
        self.manual()
        cached = self.data_dir / "catalog-renditions" / (self.full_hash + ".jpg")
        cached.write_bytes(b"corrupt cached data")
        self.inventory.return_value = [{"content_id": "MY-SYNTHETIC-1"}]
        result = self.manual()
        self.assertEqual(result[0]["status"], "displayed")
        self.assertEqual(cached.read_bytes(), self.full_frame)
        self.assertEqual(self.client.rendition.call_count, 2)
        self.upload.assert_called_once()

    def test_cache_directory_symlink_does_not_read_or_modify_external_files(self):
        with tempfile.TemporaryDirectory(prefix="frameart-external-cache-") as external:
            outside = Path(external)
            marker = outside / "unchanged.txt"
            marker.write_text("unchanged")
            (self.data_dir / "catalog-renditions").symlink_to(outside, target_is_directory=True)
            result = self.manual()
            self.assertEqual(result[0]["status"], "error")
            self.assertEqual(marker.read_text(), "unchanged")
            self.assertEqual(list(outside.iterdir()), [marker])
        self.client_type.assert_not_called()
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_cache_is_not_reused_when_resourcespace_origin_changes(self):
        self.manual()
        self.settings.resourcespace_url = "https://other-catalog.example.test"
        self.client.rendition.return_value = jpeg(color=(200, 100, 25))
        self.manual()
        self.assertEqual(self.client.rendition.call_count, 2)

    def test_bad_non_jpeg_non_rgb_and_non_4k_bytes_never_reach_tv(self):
        invalid = (
            b"not an image",
            jpeg(size=(1920, 1080)),
            jpeg(file_type="PNG"),
            jpeg(color=30, mode="L"),
        )
        for payload in invalid:
            with self.subTest(size=len(payload)):
                self.client.rendition.return_value = payload
                result = self.manual()
                self.assertEqual(result[0]["status"], "error")
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_changed_mode_at_upload_guard_defers_without_selecting(self):
        self.upload.return_value = SimpleNamespace(success=False, content_id="", deferred=True)
        result = self.manual()
        self.assertEqual(result[0]["status"], "deferred")
        self.select.assert_not_called()
        self.assertTrue(self.upload.call_args.kwargs["require_art_mode"])

    def test_unconfirmed_upload_is_uncertain_and_does_not_select(self):
        self.upload.return_value = SimpleNamespace(success=False, content_id="", deferred=False)
        result = self.manual()
        self.assertEqual(result[0]["status"], "uncertain")
        self.select.assert_not_called()

    def test_unconfirmed_select_is_uncertain_not_a_second_upload(self):
        self.select.return_value = False
        result = self.manual()
        self.assertEqual(result[0]["status"], "uncertain")
        self.upload.assert_called_once()
        self.select.assert_called_once()

    def test_skip_tvs_honored_before_status_or_fetch(self):
        self.enable_daily()
        result = self.service.deliver(
            self.artwork.id,
            "tv",
            "family-room",
            self.day_key,
            skip_tvs={"family-room"},
        )
        self.assertEqual(result["results"], [])
        self.status.assert_not_called()
        self.client_type.assert_not_called()
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_manual_hold_prevents_daily_fetch_upload_or_selection(self):
        store = self.enable_daily()
        store.note_manual_choice("family-room")
        result = self.service.deliver(
            self.artwork.id,
            "tv",
            "family-room",
            self.day_key,
            skip_tvs=set(),
        )
        self.assertEqual(result["results"][0]["status"], "deferred")
        self.status.assert_not_called()
        self.client_type.assert_not_called()
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_group_skip_leaves_only_unfinished_target_eligible(self):
        group = AutomationStore(self.data_dir).create_group(
            "Synthetic group",
            ["family-room", "bedroom"],
        )
        self.enable_daily("group", group["id"])
        result = self.service.deliver(
            self.artwork.id,
            "group",
            group["id"],
            self.day_key,
            skip_tvs={"family-room"},
        )
        self.assertEqual([row["tv"] for row in result["results"]], ["bedroom"])
        self.status.assert_called_once_with(self.bedroom)
        self.assertIs(self.upload.call_args.args[0], self.bedroom)

    def test_pause_while_rendition_fetch_is_inflight_prevents_upload_and_selection(self):
        store = self.enable_daily()

        def fetched(_resource):
            store.configure({"enabled": False})
            return self.full_frame

        self.client.rendition.side_effect = fetched
        result = self.service.deliver(
            self.artwork.id,
            "tv",
            "family-room",
            self.day_key,
            skip_tvs=set(),
        )
        self.assertEqual(result["results"][0]["status"], "deferred")
        self.client.rendition.assert_called_once()
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_new_target_while_rendition_fetch_is_inflight_prevents_old_tv_mutation(self):
        store = self.enable_daily()

        def fetched(_resource):
            store.configure({"target_id": "bedroom"})
            return self.full_frame

        self.client.rendition.side_effect = fetched
        result = self.service.deliver(
            self.artwork.id,
            "tv",
            "family-room",
            self.day_key,
            skip_tvs=set(),
        )
        self.assertEqual(result["results"][0]["status"], "deferred")
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_manual_choice_while_rendition_fetch_is_inflight_prevents_mutation(self):
        store = self.enable_daily()

        def fetched(_resource):
            store.note_manual_choice("family-room")
            return self.full_frame

        self.client.rendition.side_effect = fetched
        result = self.service.deliver(
            self.artwork.id,
            "tv",
            "family-room",
            self.day_key,
            skip_tvs=set(),
        )
        self.assertEqual(result["results"][0]["status"], "deferred")
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_new_manual_choice_after_upload_prevents_final_selection(self):
        store = self.enable_daily()

        def uploaded(*_args, **_kwargs):
            store.note_manual_choice("family-room")
            return SimpleNamespace(success=True, content_id="MY-NEW", deferred=False)

        self.upload.side_effect = uploaded
        result = self.service.deliver(
            self.artwork.id,
            "tv",
            "family-room",
            self.day_key,
            skip_tvs=set(),
        )
        self.assertEqual(result["results"][0]["status"], "deferred")
        self.upload.assert_called_once()
        self.select.assert_not_called()

    def test_retention_deletes_only_old_owned_not_unrelated_or_current_art(self):
        identities = self.seed_owned()
        self.inventory.return_value += [{"content_id": "MY-UNRELATED"}]
        self.service._prune_owned(self.settings, self.family, identities[-1])
        self.delete.assert_called_once_with(
            self.family,
            [identities[0]],
            require_art_mode=True,
        )
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            owned = connection.execute(
                "SELECT content_id FROM catalog_daily_owned WHERE tv_ip=?", (self.family.ip,)
            ).fetchall()
        self.assertEqual(len(owned), 8)
        self.assertNotIn((identities[0],), owned)

    def test_retention_does_not_delete_current_even_if_it_is_oldest(self):
        identities = self.seed_owned()
        self.service._prune_owned(self.settings, self.family, identities[0])
        self.delete.assert_not_called()

    def test_retention_preserves_recycled_id_when_actual_thumbnail_does_not_match(self):
        identities = self.seed_owned()
        self.thumbnail.side_effect = lambda _profile, identity: (
            b"different-family-photo"
            if identity == identities[0]
            else ("thumbnail:" + identity).encode()
        )
        self.service._prune_owned(self.settings, self.family, identities[-1])
        self.delete.assert_not_called()
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            owner = connection.execute(
                "SELECT 1 FROM catalog_daily_owned WHERE content_id=?", (identities[0],)
            ).fetchone()
        self.assertIsNone(owner)

    def test_retention_missing_thumbnail_never_deletes_an_unverified_tv_file(self):
        identities = self.seed_owned()
        self.thumbnail.side_effect = None
        self.thumbnail.return_value = None
        self.service._prune_owned(self.settings, self.family, identities[-1])
        self.delete.assert_not_called()

    def test_retention_pairing_epoch_change_invalidates_local_claims_without_tv_delete(self):
        identities = self.seed_owned()
        Path(self.family.token_file).write_text("new-synthetic-pairing-epoch")
        self.service._prune_owned(self.settings, self.family, identities[-1])
        self.delete.assert_not_called()
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM catalog_daily_owned").fetchone()[0], 0
            )

    def test_retention_has_at_most_eight_fresh_thumbnail_reads_per_cleanup(self):
        identities = self.seed_owned(count=25)
        self.thumbnail.reset_mock()
        self.service._prune_owned(self.settings, self.family, identities[-1])
        self.assertEqual(self.thumbnail.call_count, 8)
        self.assertEqual(self.delete.call_count, 8)

    def test_explicit_manual_selection_protects_a_former_daily_owned_image(self):
        identities = self.seed_owned(count=10)
        LibraryStore(self.data_dir).record_display(
            job_id=None,
            content_id=identities[0],
            tv_target="family-room",
            source="tv-art",
        )
        self.service._prune_owned(self.settings, self.family, identities[-1])
        self.delete.assert_called_once_with(
            self.family,
            [identities[1]],
            require_art_mode=True,
        )
        self.assertTrue(
            all(identities[0] not in call.args[1] for call in self.delete.call_args_list)
        )

    def test_retention_reconciles_reset_missing_ids_without_deleting_other_content(self):
        identities = self.seed_owned()
        self.inventory.return_value = [{"content_id": item} for item in identities[1:]]
        self.service._prune_owned(self.settings, self.family, identities[-1])
        self.delete.assert_not_called()
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            owned = connection.execute("SELECT content_id FROM catalog_daily_owned").fetchall()
        self.assertEqual(len(owned), 8)
        self.assertNotIn((identities[0],), owned)

    def test_rendition_cache_is_bounded_to_eight_files(self):
        CatalogDeliveryStore(self.data_dir)
        for index in range(9):
            artwork = self.artwork.model_copy(
                update={"id": f"art-{index}", "rendition_ref": index + 1}
            )
            self.client.rendition.return_value = jpeg(color=(index * 20, 35, 55))
            self.service._rendition(self.settings, artwork)
        with sqlite3.connect(self.data_dir / "frameart.sqlite3") as connection:
            rows = connection.execute("SELECT artwork_id FROM catalog_renditions").fetchall()
        self.assertEqual(len(rows), 8)
        self.assertNotIn(("art-0",), rows)
        directory = self.data_dir / "catalog-renditions"
        self.assertEqual(len(list(directory.glob("*.jpg"))), 8)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue(
            all(path.stat().st_mode & 0o777 == 0o600 for path in directory.glob("*.jpg"))
        )

    def test_jobs_are_claimed_once_and_interrupted_job_is_never_replayed(self):
        store = CatalogDeliveryStore(self.data_dir)
        queued = store.enqueue(self.artwork.id, ["family-room"])
        self.assertEqual(queued["status"], "queued")
        claimed = store.claim()
        self.assertEqual(claimed["id"], queued["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertIsNone(CatalogDeliveryStore(self.data_dir).claim())
        reopened = CatalogDeliveryStore(self.data_dir)
        reopened.recover()
        result = reopened.get(queued["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["results"][0]["status"], "uncertain")
        self.assertIsNone(reopened.claim())
        self.client_type.assert_not_called()
        self.upload.assert_not_called()
        self.select.assert_not_called()

    def test_queued_job_survives_restart_without_a_duplicate(self):
        store = CatalogDeliveryStore(self.data_dir)
        queued = store.enqueue(self.artwork.id, ["family-room"])
        reopened = CatalogDeliveryStore(self.data_dir)
        reopened.recover()
        self.assertEqual(reopened.claim()["id"], queued["id"])
        self.assertIsNone(reopened.claim())

    def test_enqueue_rejects_unknown_artwork_and_invalid_saved_targets(self):
        with self.assertRaises(ValueError):
            self.service.enqueue("not-in-catalog", "tv", "family-room")
        with self.assertRaises(ValueError):
            self.service.enqueue(self.artwork.id, "tv", "192.0.2.99")
        with self.assertRaises(ValueError):
            resolve_targets(self.settings, "all", "family-room")
        self.upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
