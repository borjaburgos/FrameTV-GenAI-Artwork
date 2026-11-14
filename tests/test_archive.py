"""Archive safety, failure recovery and byte-for-byte offline restore tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import frameart.archive as archive
from frameart.cli import main
from frameart.config import ProviderConfig, Settings

KEY = "2026/11/14/120000-aabbccdd"


@pytest.fixture
def setup_archive(tmp_path, monkeypatch):
    data = tmp_path / "data"
    nas = tmp_path / "nas"
    nas.mkdir()
    (nas / ".frameart-archive-id").write_text("test-archive")
    settings = Settings(
        data_dir=data,
        archive_enabled=True,
        archive_path=nas,
        archive_mount_source="//nas/Generated",
        archive_id="test-archive",
    )
    monkeypatch.setattr(archive, "_mounts", lambda: [(nas, "cifs", "//nas/Generated")])
    job = data / "artifacts" / KEY
    job.mkdir(parents=True)
    (job / "source.png").write_bytes(b"original-provider-bytes")
    (job / "final.png").write_bytes(b"full-frame-TV-ready-bytes")
    (job / "meta.json").write_text(
        json.dumps(
            {
                "job_id": job.name,
                "prompt_original": "Bold summer shapes",
                "provider": "openai",
                "model": "image-model",
                "seed": 42,
            }
        )
    )
    return settings, job, nas


def test_copy_verify_restore_is_byte_identical_and_idempotent(setup_archive, tmp_path):
    settings, job, nas = setup_archive
    original = {path.name: path.read_bytes() for path in job.iterdir()}
    store = archive.ArchiveStore(settings)
    assert store.stage(job) == KEY
    assert store.status()["pending"] == 1
    assert not (nas / KEY).exists()
    assert store.run()["verified"] == 1
    assert not (store.outbox / KEY).exists()
    assert store.verify(KEY)["verified"]
    destination = tmp_path / "offline-restore"
    assert store.restore(KEY, destination)["restored"]
    for name in ("source.png", "final.png"):
        assert (destination / name).read_bytes() == original[name]
    assert json.loads((destination / "meta.json").read_text())["seed"] == 42
    assert {path.name: path.read_bytes() for path in job.iterdir()} == original
    before = (nas / KEY / "manifest.json").stat().st_mtime_ns
    assert store.run()["verified"] == 1
    assert (nas / KEY / "manifest.json").stat().st_mtime_ns == before


def test_outage_retries_after_source_library_deletion(setup_archive, monkeypatch):
    settings, job, nas = setup_archive
    store = archive.ArchiveStore(settings)
    archive.protect_before_delete(settings, job)
    shutil.rmtree(job)
    with patch.object(archive, "_mounts", return_value=[]):
        status = store.run(discover=False)
    assert status["failed"] == 1
    assert status["errors"] == {"nas_unavailable": 1}
    assert (store.outbox / KEY / "source.png").read_bytes() == b"original-provider-bytes"
    assert not (nas / KEY).exists()
    assert store.run(discover=False)["failed"] == 1  # retry delay is respected
    assert store.run(discover=False, retry_now=True)["verified"] == 1
    assert (nas / KEY / "final.png").read_bytes() == b"full-frame-TV-ready-bytes"


@pytest.mark.parametrize(
    "bad_mount",
    [[], [(Path("/"), "ext4", "//nas/Generated")], [(Path("/"), "cifs", "//wrong/Generated")]],
)
def test_missing_wrong_or_local_mount_never_creates_archive(setup_archive, monkeypatch, bad_mount):
    settings, job, nas = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    monkeypatch.setattr(archive, "_mounts", lambda: bad_mount)
    assert store.run(discover=False)["failed"] == 1
    assert not (nas / KEY).exists()


@pytest.mark.parametrize("marker", ["wrong", "x" * 257, None])
def test_missing_or_wrong_marker_is_rejected(setup_archive, marker):
    settings, job, nas = setup_archive
    marker_path = nas / ".frameart-archive-id"
    if marker is None:
        marker_path.unlink()
    else:
        marker_path.write_text(marker)
    store = archive.ArchiveStore(settings)
    store.stage(job)
    assert store.run(discover=False)["failed"] == 1
    assert not (nas / KEY).exists()


def test_conflict_is_visible_and_never_overwritten(setup_archive):
    settings, job, nas = setup_archive
    destination = nas / KEY
    destination.mkdir(parents=True)
    (destination / "final.png").write_bytes(b"a different preexisting image")
    store = archive.ArchiveStore(settings)
    store.stage(job)
    result = store.run(discover=False)
    assert result["errors"] == {"conflict": 1}
    assert (destination / "final.png").read_bytes() == b"a different preexisting image"
    assert not (destination / "manifest.json").exists()
    assert (store.outbox / KEY).is_dir()


def test_partial_transfer_retries_without_a_false_receipt(setup_archive):
    settings, job, nas = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    copy = archive._copy_immutable
    calls = 0

    def fail_once(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("network disconnected")
        return copy(source, destination)

    with patch.object(archive, "_copy_immutable", side_effect=fail_once):
        assert store.run(discover=False)["verified"] == 0
    assert not (nas / KEY / "manifest.json").exists()
    assert store.run(discover=False, retry_now=True)["verified"] == 1


def test_corrupted_transfer_is_not_published(setup_archive):
    settings, job, nas = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    with patch.object(
        archive.shutil, "copyfileobj", side_effect=lambda _in, out, _size: out.write(b"corrupted")
    ):
        assert store.run(discover=False)["verified"] == 0
    assert not (nas / KEY / "final.png").exists()
    assert not list(nas.rglob(".frameart-partial-*"))


def test_allowlist_omits_credentials_and_unrelated_files(setup_archive):
    settings, job, nas = setup_archive
    settings.providers = {"openai": ProviderConfig(api_key="secret-provider-credential")}
    raw = json.loads((job / "meta.json").read_text())
    raw.update(
        api_key="secret-provider-credential",
        raw_response={"token": "dont-copy"},
        prompt_original="A moon secret-provider-credential",
        tv_ip="10.1.20.74",
        model="https://example.test/model?token=dont-copy",
        postprocess_steps=["crop", "secret-provider-credential"],
    )
    (job / "meta.json").write_text(json.dumps(raw))
    (job / "config.yaml").write_text("password: dont-copy")
    (job / "tv.token").write_text("dont-copy")
    store = archive.ArchiveStore(settings)
    store.stage(job)
    assert store.run(discover=False)["verified"] == 1
    metadata = (nas / KEY / "meta.json").read_text()
    assert "secret-provider-credential" not in metadata
    assert "dont-copy" not in metadata
    assert "tv_ip" not in metadata
    assert "raw_response" not in metadata
    assert json.loads(metadata)["postprocess_steps"] == ["crop", "[redacted]"]
    assert not (nas / KEY / "config.yaml").exists()
    assert not (nas / KEY / "tv.token").exists()


def test_curated_import_album_and_incomplete_jobs_are_not_archived(setup_archive):
    settings, job, _ = setup_archive
    store = archive.ArchiveStore(settings)
    for raw in ({"source_metadata": {"provider": "museum"}}, {"operation": "import"}, []):
        (job / "meta.json").write_text(json.dumps(raw))
        assert store.backfill() == {"eligible": 0, "skipped": 1, "errors": 0}
    (job / "meta.json").write_text(json.dumps({"provider": "openai", "prompt_original": "Sun"}))
    (job / "final.png").unlink()
    assert store.backfill()["errors"] == 1
    assert store.status()["pending"] == 0


def test_dry_run_does_not_queue_or_touch_nas(setup_archive):
    settings, _, nas = setup_archive
    store = archive.ArchiveStore(settings)
    with patch.object(archive, "_nas", side_effect=AssertionError("NAS must not be contacted")):
        assert store.backfill(dry_run=True) == {"eligible": 1, "skipped": 0, "errors": 0}
        assert store.status()["pending"] == 0
    assert not (nas / KEY).exists()


@pytest.mark.parametrize("name", ["source.png", "final.png", "meta.json"])
def test_source_symlinks_are_rejected(setup_archive, tmp_path, name):
    settings, job, _ = setup_archive
    other = tmp_path / "private"
    other.write_bytes((job / name).read_bytes())
    (job / name).unlink()
    (job / name).symlink_to(other)
    assert archive.ArchiveStore(settings).backfill()["errors"] == 1


def test_destination_symlink_cannot_escape_archive(setup_archive, tmp_path):
    settings, job, nas = setup_archive
    outside = tmp_path / "outside"
    outside.mkdir()
    (nas / "2026").symlink_to(outside, target_is_directory=True)
    store = archive.ArchiveStore(settings)
    store.stage(job)
    assert store.run(discover=False)["failed"] == 1
    assert not list(outside.iterdir())


def test_corruption_and_manifest_tampering_block_restore(setup_archive, tmp_path):
    settings, job, nas = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    store.run(discover=False)
    (nas / KEY / "source.png").write_bytes(b"corrupted")
    with pytest.raises(archive.ArchiveConflictError):
        store.restore(KEY, tmp_path / "restore-corrupt")
    assert not (tmp_path / "restore-corrupt").exists()


@pytest.mark.parametrize(
    "key",
    [
        "../../secrets",
        "2026/11/14/../../secret",
        "/etc/passwd",
        "2026/19/14/job",
        "2026/11/14/job/secret",
    ],
)
def test_restore_rejects_unsafe_keys(setup_archive, tmp_path, key):
    settings, _, _ = setup_archive
    with pytest.raises(ValueError):
        archive.ArchiveStore(settings).restore(key, tmp_path / "restore")


def test_restore_requires_new_offline_directory(setup_archive, tmp_path):
    settings, job, _ = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    store.run(discover=False)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("unchanged")
    for output in (existing, settings.data_dir / "artifacts" / "new"):
        with pytest.raises(ValueError):
            store.restore(KEY, output)
    assert (existing / "keep").read_text() == "unchanged"


def test_no_overwrite_publication_even_when_destination_appears_during_copy(tmp_path):
    source, destination = tmp_path / "stage", tmp_path / "existing"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        archive._publish(source, destination)
    assert destination.read_bytes() == b"old"
    assert source.read_bytes() == b"new"


def test_worker_lock_prevents_overlapping_copies(setup_archive):
    settings, _, _ = setup_archive
    store = archive.ArchiveStore(settings)
    with store._lock("worker.lock"), pytest.raises(BlockingIOError):
        store.run(discover=False)


def test_local_enqueue_failure_is_not_a_generation_failure(setup_archive):
    settings, job, _ = setup_archive
    with patch.object(archive.ArchiveStore, "stage", side_effect=OSError("disk full")):
        archive.enqueue_generation(settings, job)
        with pytest.raises(OSError):
            archive.protect_before_delete(settings, job)


def test_verified_cleanup_failure_does_not_revoke_receipt(setup_archive):
    settings, job, _ = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    with patch.object(archive.shutil, "rmtree", side_effect=OSError("local cleanup error")):
        assert store.run(discover=False)["verified"] == 1
    assert store.status()["failed"] == 0
    assert (store.outbox / KEY).exists()
    assert store.run(discover=False)["verified"] == 1
    assert not (store.outbox / KEY).exists()


def test_generate_only_stages_locally_and_nas_failure_cannot_affect_result(setup_archive):
    from frameart.pipeline import run_generate

    settings, _, _ = setup_archive
    provider = SimpleNamespace(
        name="openai",
        generate=lambda *_args, **_kwargs: SimpleNamespace(
            data=b"raw-generated", width=4, height=4, metadata={}
        ),
    )
    processed = SimpleNamespace(image_bytes=b"TV-generated", width=3840, height=2160, steps=[])
    with (
        patch("frameart.pipeline._get_provider_instance", return_value=provider),
        patch(
            "frameart.pipeline._get_upscaler_instance", return_value=SimpleNamespace(name="none")
        ),
        patch("frameart.pipeline.postprocess", return_value=processed),
        patch.object(archive, "_nas", side_effect=AssertionError("No NAS from generation")),
    ):
        result = run_generate(settings, "Bold shapes", seed=123)
        assert result.error is None
        assert archive.ArchiveStore(settings).status()["pending"] == 1
        with patch.object(archive.ArchiveStore, "stage", side_effect=OSError("disk full")):
            result = run_generate(settings, "Still succeeds")
            assert result.error is None


def test_source_deletion_never_deletes_nas_archive(setup_archive):
    settings, job, nas = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    store.run(discover=False)
    archive.protect_before_delete(settings, job)
    shutil.rmtree(job)
    store.run()
    assert store.verify(KEY)["verified"]
    assert (nas / KEY / "source.png").exists()


def test_fresh_data_directory_can_restore_after_host_loss(setup_archive, tmp_path):
    settings, job, _ = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    store.run(discover=False)
    fresh = settings.model_copy(update={"data_dir": tmp_path / "replacement-host-data"})
    restored = tmp_path / "recovered"
    assert archive.ArchiveStore(fresh).restore(KEY, restored)["restored"]
    assert (restored / "source.png").read_bytes() == (job / "source.png").read_bytes()


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {},
        {"format_version": 999},
        {
            "format_version": 1,
            "generation_key": KEY,
            "files": {"../private": {"bytes": 0, "sha256": "a" * 64}},
        },
    ],
)
def test_malformed_manifests_fail_closed(setup_archive, tmp_path, manifest):
    settings, job, nas = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    store.run(discover=False)
    (nas / KEY / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        store.restore(KEY, tmp_path / "unsafe-restore")
    assert not (tmp_path / "unsafe-restore").exists()


def test_verified_receipt_detects_self_consistent_manifest_tampering(setup_archive, tmp_path):
    settings, job, nas = setup_archive
    store = archive.ArchiveStore(settings)
    store.stage(job)
    store.run(discover=False)
    (nas / KEY / "final.png").write_bytes(b"changed image")
    manifest = json.loads((nas / KEY / "manifest.json").read_text())
    manifest["files"]["final.png"] = {
        "sha256": archive._digest(nas / KEY / "final.png"),
        "bytes": len(b"changed image"),
    }
    (nas / KEY / "manifest.json").write_bytes(archive._json_bytes(manifest))
    with pytest.raises(archive.ArchiveConflictError):
        store.restore(KEY, tmp_path / "tampered-restore")


def test_queue_and_snapshot_survive_store_restart(setup_archive):
    settings, job, _ = setup_archive
    archive.ArchiveStore(settings).stage(job)
    restarted = archive.ArchiveStore(settings)
    assert restarted.status()["pending"] == 1
    assert restarted.run(discover=False)["verified"] == 1


def test_api_status_is_local_and_api_delete_preserves_outbox(setup_archive):
    from frameart import api

    settings, job, _ = setup_archive
    with (
        patch.object(api, "_settings", return_value=settings),
        patch.object(api, "_library_store"),
        patch.object(api, "_configured_job_store"),
        patch.object(archive, "_nas", side_effect=AssertionError("Web must not contact NAS")),
    ):
        assert api.archive_status()["pending"] == 0
        response = api.delete_jobs(api.DeleteJobsRequest(job_ids=[job.name]))
        assert response.deleted == [job.name]
        assert not job.exists()
        assert (archive.ArchiveStore(settings).outbox / KEY / "source.png").exists()


def test_api_delete_refuses_to_lose_unstaged_artwork(setup_archive):
    from frameart import api

    settings, job, _ = setup_archive
    with (
        patch.object(api, "_settings", return_value=settings),
        patch.object(api, "_library_store"),
        patch.object(api, "_configured_job_store"),
        patch.object(archive.ArchiveStore, "stage", side_effect=OSError("disk full")),
    ):
        response = api.delete_jobs(api.DeleteJobsRequest(job_ids=[job.name]))
    assert response.deleted == []
    assert job.name in response.failed
    assert (job / "final.png").is_file()


def test_edit_archives_raw_edit_and_original_even_if_tv_delivery_fails(setup_archive):
    from frameart.pipeline import run_edit_and_apply

    settings, job, _ = setup_archive
    fake_provider = SimpleNamespace(
        name="test",
        edit=lambda *_args, **_kwargs: SimpleNamespace(
            data=b"raw-edit", width=4, height=4, metadata={}
        ),
    )
    processed = SimpleNamespace(image_bytes=b"TV-edit", width=3840, height=2160, steps=[])
    with (
        patch("frameart.pipeline._get_provider_instance", return_value=fake_provider),
        patch(
            "frameart.pipeline._get_upscaler_instance", return_value=SimpleNamespace(name="none")
        ),
        patch("frameart.pipeline.postprocess", return_value=processed),
    ):
        result = run_edit_and_apply(
            settings, job / "source.png", "Make abstract", tv_name="missing"
        )
    assert result.error
    store = archive.ArchiveStore(settings)
    assert store.status()["pending"] == 1
    key = result.job_dir.relative_to(settings.data_dir / "artifacts").as_posix()
    assert (store.outbox / key / "edited.png").read_bytes() == b"raw-edit"
    assert (store.outbox / key / "source.png").read_bytes() == b"original-provider-bytes"


def test_cli_dry_run_and_disabled_run_are_explicit(setup_archive, monkeypatch):
    settings, _, _ = setup_archive
    monkeypatch.setenv("FRAMEART_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("FRAMEART_CONFIG", str(settings.data_dir / "absent.yaml"))
    monkeypatch.setenv("FRAMEART_ARCHIVE_ENABLED", "false")
    runner = CliRunner()
    result = runner.invoke(main, ["archive", "backfill", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["eligible"] == 1
    result = runner.invoke(main, ["archive", "run"])
    assert result.exit_code == 1
    assert "ValueError" in result.output
