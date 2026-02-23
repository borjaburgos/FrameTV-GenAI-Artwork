"""Tests for artifact store."""

from __future__ import annotations

from frameart.artifacts import (
    generate_job_id,
    get_job_dir,
    load_metadata,
    save_final_image,
    save_metadata,
    save_source_image,
)


class TestGenerateJobId:
    def test_format(self):
        jid = generate_job_id()
        # Format: HHMMSS-xxxxxxxx
        assert "-" in jid
        parts = jid.split("-")
        assert len(parts) == 2
        assert len(parts[0]) == 6  # HHMMSS
        assert len(parts[1]) == 8  # hex

    def test_unique(self):
        ids = {generate_job_id() for _ in range(100)}
        assert len(ids) == 100


class TestArtifactStore:
    def test_get_job_dir(self, tmp_path):
        job_dir = get_job_dir(tmp_path, "test-job")
        assert job_dir.exists()
        assert job_dir.is_dir()
        assert "test-job" in str(job_dir)

    def test_save_and_load(self, tmp_path):
        job_dir = get_job_dir(tmp_path, "test-job")

        # Save source
        src = save_source_image(job_dir, b"\x89PNG_fake_source")
        assert src.exists()
        assert src.read_bytes() == b"\x89PNG_fake_source"

        # Save final
        final = save_final_image(job_dir, b"\x89PNG_fake_final")
        assert final.exists()
        assert final.read_bytes() == b"\x89PNG_fake_final"

        # Save metadata
        meta = {"job_id": "test-job", "prompt": "test prompt"}
        meta_path = save_metadata(job_dir, meta)
        assert meta_path.exists()

        # Load metadata
        loaded = load_metadata(job_dir)
        assert loaded["job_id"] == "test-job"
        assert loaded["prompt"] == "test prompt"
