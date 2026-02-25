"""Tests for the FrameArt HTTP API."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from frameart.api import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@dataclass
class FakePipelineResult:
    job_id: str = "120000-abcd1234"
    job_dir: Path = Path("/tmp/fakejob")
    source_path: Path | None = Path("/tmp/fakejob/source.png")
    final_path: Path | None = Path("/tmp/fakejob/final.png")
    content_id: str | None = "MY_ART_001"
    tv_switched: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=lambda: {"generation_ms": 5000.0})
    error: str | None = None


def _fake_result(**overrides) -> FakePipelineResult:
    return FakePipelineResult(**overrides)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data


# ---------------------------------------------------------------------------
# /styles
# ---------------------------------------------------------------------------

class TestStyles:
    def test_list_styles(self):
        resp = client.get("/styles")
        assert resp.status_code == 200
        data = resp.json()
        assert "abstract" in data
        assert "watercolor" in data


# ---------------------------------------------------------------------------
# POST /generate
# ---------------------------------------------------------------------------

class TestGenerate:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate")
    def test_success(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post("/generate", json={"prompt": "a sunset over the ocean"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "120000-abcd1234"
        assert data["content_id"] == "MY_ART_001"
        assert data["error"] is None

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs[0][1] == "a sunset over the ocean"

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate")
    def test_with_style(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post("/generate", json={"prompt": "a cat", "style": "abstract"})
        assert resp.status_code == 200
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["style"] == "abstract"

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate")
    def test_pipeline_error_returns_500(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result(error="API key invalid")

        resp = client.post("/generate", json={"prompt": "test"})
        assert resp.status_code == 500
        data = resp.json()["detail"]
        assert data["error"] == "API key invalid"

    def test_missing_prompt_returns_422(self):
        resp = client.post("/generate", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /generate-and-apply
# ---------------------------------------------------------------------------

class TestGenerateAndApply:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate_and_apply")
    def test_success(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/generate-and-apply",
            json={"prompt": "a mountain landscape", "matte": "modern_black"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tv_switched"] is True
        assert data["content_id"] == "MY_ART_001"

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["matte"] == "modern_black"

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate_and_apply")
    def test_with_tv_ip(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/generate-and-apply",
            json={"prompt": "flowers", "tv_ip": "192.168.1.100"},
        )
        assert resp.status_code == 200
        assert mock_run.call_args.kwargs["tv_ip"] == "192.168.1.100"

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate_and_apply")
    def test_no_switch(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result(tv_switched=False)

        resp = client.post(
            "/generate-and-apply",
            json={"prompt": "test", "no_switch": True},
        )
        assert resp.status_code == 200
        assert mock_run.call_args.kwargs["no_switch"] is True

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate_and_apply")
    def test_default_matte_is_none(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/generate-and-apply",
            json={"prompt": "test without explicit matte"},
        )
        assert resp.status_code == 200
        assert mock_run.call_args.kwargs["matte"] == "none"


# ---------------------------------------------------------------------------
# POST /apply
# ---------------------------------------------------------------------------

class TestApply:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_apply")
    def test_success(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/apply",
            json={"image_path": "/tmp/test.png", "tv_ip": "192.168.1.50"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content_id"] == "MY_ART_001"

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_apply")
    def test_default_matte_is_none(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/apply",
            json={"image_path": "/tmp/test.png", "tv_ip": "192.168.1.50"},
        )
        assert resp.status_code == 200
        assert mock_run.call_args.kwargs["matte"] == "none"

    def test_missing_image_path_returns_422(self):
        resp = client.post("/apply", json={"tv_ip": "192.168.1.50"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /tv/status
# ---------------------------------------------------------------------------

class TestTVStatus:
    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.get_status")
    def test_with_tv_ip(self, mock_status, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        mock_status.return_value = MagicMock(
            reachable=True,
            art_mode_supported=True,
            art_mode_on=True,
            current_artwork="MY_ART_001",
            error=None,
        )

        resp = client.get("/tv/status?tv_ip=192.168.1.100")
        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is True
        assert data["art_mode_on"] is True

    @patch("frameart.api._settings")
    def test_no_tv_returns_400(self, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        resp = client.get("/tv/status")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /jobs
# ---------------------------------------------------------------------------

class TestListJobs:
    @patch("frameart.api._settings")
    def test_empty(self, mock_settings):
        settings = MagicMock()
        settings.data_dir = Path("/tmp/nonexistent_frameart_test")
        mock_settings.return_value = settings

        resp = client.get("/jobs")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("frameart.api._settings")
    def test_skips_jobs_without_preview_image(self, mock_settings):
        import tempfile

        settings = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts = Path(tmpdir) / "artifacts" / "2025" / "01" / "01"
            with_preview = artifacts / "with-preview"
            no_preview = artifacts / "no-preview"
            with_preview.mkdir(parents=True)
            no_preview.mkdir(parents=True)

            (with_preview / "meta.json").write_text(
                '{"job_id":"with-preview","provider":"openai"}'
            )
            (with_preview / "final.png").write_bytes(b"fakepng")
            (no_preview / "meta.json").write_text('{"job_id":"no-preview"}')

            settings.data_dir = Path(tmpdir)
            mock_settings.return_value = settings

            resp = client.get("/jobs")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["job_id"] == "with-preview"


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/image
# ---------------------------------------------------------------------------

class TestGetJobImage:
    @patch("frameart.api._settings")
    def test_not_found(self, mock_settings):
        settings = MagicMock()
        settings.data_dir = Path("/tmp/nonexistent_frameart_test")
        mock_settings.return_value = settings

        resp = client.get("/jobs/doesnotexist/image")
        assert resp.status_code == 404

    @patch("frameart.api._settings")
    def test_falls_back_to_source_image(self, mock_settings):
        import tempfile

        settings = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "artifacts" / "2025" / "01" / "01" / "job-1"
            job_dir.mkdir(parents=True)
            (job_dir / "source.png").write_bytes(b"\x89PNG\r\n")
            settings.data_dir = Path(tmpdir)
            mock_settings.return_value = settings

            resp = client.get("/jobs/job-1/image")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "image/png"


# ---------------------------------------------------------------------------
# POST /jobs/delete
# ---------------------------------------------------------------------------

class TestDeleteJobs:
    @patch("frameart.api._settings")
    def test_deletes_existing_job_dir(self, mock_settings):
        import tempfile

        settings = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "artifacts" / "2025" / "01" / "01" / "test-job"
            job_dir.mkdir(parents=True)
            (job_dir / "meta.json").write_text('{"job_id":"test-job"}')
            (job_dir / "final.png").write_bytes(b"fakepng")
            settings.data_dir = Path(tmpdir)
            mock_settings.return_value = settings

            resp = client.post("/jobs/delete", json={"job_ids": ["test-job"]})
            assert resp.status_code == 200
            data = resp.json()
            assert data["deleted"] == ["test-job"]
            assert data["not_found"] == []
            assert data["failed"] == {}
            assert not job_dir.exists()

    @patch("frameart.api._settings")
    def test_returns_not_found_for_missing_job(self, mock_settings):
        settings = MagicMock()
        settings.data_dir = Path("/tmp/nonexistent_frameart_test")
        mock_settings.return_value = settings

        resp = client.post("/jobs/delete", json={"job_ids": ["does-not-exist"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == []
        assert data["not_found"] == ["does-not-exist"]
        assert data["failed"] == {}

    @patch("frameart.api._settings")
    @patch("frameart.api.shutil")
    def test_reports_failed_deletions(self, mock_shutil, mock_settings):
        import tempfile

        settings = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "artifacts" / "2025" / "01" / "01" / "test-job"
            job_dir.mkdir(parents=True)
            (job_dir / "meta.json").write_text('{"job_id":"test-job"}')
            settings.data_dir = Path(tmpdir)
            mock_settings.return_value = settings
            mock_shutil.rmtree.side_effect = OSError("permission denied")

            resp = client.post("/jobs/delete", json={"job_ids": ["test-job"]})
            assert resp.status_code == 200
            data = resp.json()
            assert data["deleted"] == []
            assert data["not_found"] == []
            assert "test-job" in data["failed"]


# ---------------------------------------------------------------------------
# GET /tv/discover
# ---------------------------------------------------------------------------

class TestTVDiscover:
    @patch("frameart.tv.discovery.discover")
    def test_returns_tvs(self, mock_discover):
        from frameart.tv.discovery import DiscoveredTV

        mock_discover.return_value = [
            DiscoveredTV(ip="10.0.0.1", name="LivingRoom", model="QN55LS03", frame_tv=True),
        ]

        resp = client.get("/tv/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["ip"] == "10.0.0.1"
        assert data[0]["frame_tv"] is True

    @patch("frameart.tv.discovery.discover")
    def test_empty(self, mock_discover):
        mock_discover.return_value = []
        resp = client.get("/tv/discover")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /tv/art
# ---------------------------------------------------------------------------

class TestTVListArt:
    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.list_art_deduplicated")
    def test_returns_deduplicated_list(self, mock_list, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        mock_list.return_value = [
            {"content_id": "MY_F0001", "is_favourite": True},
            {"content_id": "MY_F0002", "is_favourite": False},
        ]

        resp = client.get("/tv/art?tv_ip=192.168.1.100")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["content_id"] == "MY_F0001"
        assert data[0]["is_favourite"] is True
        assert data[1]["is_favourite"] is False

    @patch("frameart.api._settings")
    def test_no_tv_returns_400(self, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        resp = client.get("/tv/art")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /tv/art/thumbnail
# ---------------------------------------------------------------------------

class TestTVArtThumbnail:
    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.get_art_thumbnail")
    def test_returns_thumbnail_bytes(self, mock_thumb, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_thumb.return_value = b"\xff\xd8\xff\xd9"

        resp = client.get("/tv/art/thumbnail?tv_ip=192.168.1.100&content_id=MY_F0001")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == b"\xff\xd8\xff\xd9"

    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.get_art_thumbnail")
    def test_returns_404_when_unavailable(self, mock_thumb, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_thumb.return_value = None

        resp = client.get("/tv/art/thumbnail?tv_ip=192.168.1.100&content_id=MY_F0001")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /tv/art/delete
# ---------------------------------------------------------------------------

class TestTVDeleteArt:
    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.delete_art")
    @patch("frameart.tv.controller.list_art_deduplicated")
    def test_skips_favorites_by_default(self, mock_list, mock_delete, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        mock_list.return_value = [
            {"content_id": "MY_F0001", "is_favourite": True},
            {"content_id": "MY_F0002", "is_favourite": False},
        ]
        mock_delete.return_value = True

        resp = client.post("/tv/art/delete", json={
            "content_ids": ["MY_F0001", "MY_F0002"],
            "tv_ip": "192.168.1.100",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "MY_F0001" in data["skipped_favorites"]
        assert "MY_F0002" in data["deleted"]
        mock_delete.assert_called_once_with(mock_delete.call_args[0][0], ["MY_F0002"])

    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.delete_art")
    @patch("frameart.tv.controller.list_art_deduplicated")
    def test_include_favorites(self, mock_list, mock_delete, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        mock_list.return_value = [
            {"content_id": "MY_F0001", "is_favourite": True},
        ]
        mock_delete.return_value = True

        resp = client.post("/tv/art/delete", json={
            "content_ids": ["MY_F0001"],
            "tv_ip": "192.168.1.100",
            "include_favorites": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["skipped_favorites"] == []
        assert "MY_F0001" in data["deleted"]

    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.list_art_deduplicated")
    def test_all_favorites_skipped_returns_empty(self, mock_list, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        mock_list.return_value = [
            {"content_id": "MY_F0001", "is_favourite": True},
        ]

        resp = client.post("/tv/art/delete", json={
            "content_ids": ["MY_F0001"],
            "tv_ip": "192.168.1.100",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == []
        assert data["skipped_favorites"] == ["MY_F0001"]

    @patch("frameart.api._settings")
    def test_no_tv_returns_400(self, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        resp = client.post("/tv/art/delete", json={
            "content_ids": ["MY_F0001"],
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /tv/art/matte
# ---------------------------------------------------------------------------

class TestTVChangeMatte:
    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.change_matte")
    def test_success(self, mock_change, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_change.return_value = True

        resp = client.post("/tv/art/matte", json={
            "content_id": "MY_F0001",
            "matte_id": "shadowbox_noir",
            "tv_ip": "192.168.1.100",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["content_id"] == "MY_F0001"
        assert data["matte_id"] == "shadowbox_noir"

    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.change_matte")
    def test_failure_returns_500(self, mock_change, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_change.return_value = False

        resp = client.post("/tv/art/matte", json={
            "content_id": "MY_F0001",
            "matte_id": "shadowbox_noir",
            "tv_ip": "192.168.1.100",
        })
        assert resp.status_code == 500

    @patch("frameart.api._settings")
    def test_no_tv_returns_400(self, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        resp = client.post("/tv/art/matte", json={
            "content_id": "MY_F0001",
            "matte_id": "shadowbox_noir",
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /tv/art/display
# ---------------------------------------------------------------------------

class TestTVDisplayArt:
    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.switch_art")
    def test_success(self, mock_switch, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_switch.return_value = True

        resp = client.post("/tv/art/display", json={
            "content_id": "MY_F0001",
            "tv_ip": "192.168.1.100",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["content_id"] == "MY_F0001"

    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.switch_art")
    def test_failure_returns_500(self, mock_switch, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_switch.return_value = False

        resp = client.post("/tv/art/display", json={
            "content_id": "MY_F0001",
            "tv_ip": "192.168.1.100",
        })
        assert resp.status_code == 500

    @patch("frameart.api._settings")
    def test_no_tv_returns_400(self, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        resp = client.post("/tv/art/display", json={
            "content_id": "MY_F0001",
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /tv/mattes
# ---------------------------------------------------------------------------

class TestTVMattes:
    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.get_matte_list")
    def test_returns_mattes(self, mock_mattes, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_mattes.return_value = [
            {"matte_id": "shadowbox_polar"},
            {"matte_id": "shadowbox_noir"},
        ]

        resp = client.get("/tv/mattes?tv_ip=192.168.1.100")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["matte_id"] == "shadowbox_polar"

    @patch("frameart.api._settings")
    def test_no_tv_returns_400(self, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        resp = client.get("/tv/mattes")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /tv/configured
# ---------------------------------------------------------------------------

class TestTVConfigured:
    @patch("frameart.api._settings")
    def test_returns_configured_tvs(self, mock_settings):
        from frameart.config import TVProfile

        settings = MagicMock()
        settings.tvs = {
            "living_room": TVProfile(ip="192.168.1.50", name="LivingRoom"),
            "bedroom": TVProfile(ip="192.168.1.51", name="Bedroom"),
        }
        mock_settings.return_value = settings

        resp = client.get("/tv/configured")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["name"] == "living_room"
        assert data[0]["ip"] == "192.168.1.50"

    @patch("frameart.api._settings")
    def test_empty(self, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings

        resp = client.get("/tv/configured")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Public domain catalog
# ---------------------------------------------------------------------------

class TestCatalogSearch:
    @patch("frameart.api.public_domain.search_artworks")
    def test_returns_results(self, mock_search):
        mock_search.return_value = [
            {
                "source": "met",
                "artwork_id": "123",
                "title": "Water Lilies",
                "artist": "Claude Monet",
                "date": "1906",
                "image_url": "https://example.com/full.jpg",
                "thumbnail_url": "https://example.com/thumb.jpg",
                "license": "Public Domain",
                "attribution": "The Met",
                "source_url": "https://example.com/object/123",
                "is_public_domain": True,
            }
        ]

        resp = client.get("/catalog/search?source=met&q=monet&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["source"] == "met"
        assert data[0]["artwork_id"] == "123"

    @patch("frameart.api.public_domain.search_artworks")
    def test_returns_cma_results(self, mock_search):
        mock_search.return_value = [
            {
                "source": "cma",
                "artwork_id": "98765",
                "title": "The Red Kerchief",
                "artist": "Paul Klee",
                "date": "1933",
                "image_url": "https://images.clevelandart.org/test.jpg",
                "thumbnail_url": "https://images.clevelandart.org/test-thumb.jpg",
                "license": "CC0",
                "attribution": "Cleveland Museum of Art",
                "source_url": "https://www.clevelandart.org/art/98765",
                "is_public_domain": True,
            }
        ]

        resp = client.get("/catalog/search?source=cma&q=klee&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["source"] == "cma"
        assert data[0]["artwork_id"] == "98765"

    @patch("frameart.api.public_domain.search_artworks")
    def test_returns_europeana_results(self, mock_search):
        mock_search.return_value = [
            {
                "source": "europeana",
                "artwork_id": "/90402/https___www_europeana_eu_item_test_123",
                "title": "Study for a Landscape",
                "artist": "Unknown",
                "date": "19th century",
                "image_url": "https://example.com/eu-full.jpg",
                "thumbnail_url": "https://example.com/eu-thumb.jpg",
                "license": "See source",
                "attribution": "Europeana",
                "source_url": "https://www.europeana.eu/item/test/123",
                "is_public_domain": True,
            }
        ]

        resp = client.get("/catalog/search?source=europeana&q=landscape&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["source"] == "europeana"

    @patch("frameart.api.public_domain.search_artworks")
    def test_bad_source_returns_400(self, mock_search):
        mock_search.side_effect = ValueError(
            "Unsupported source 'foo'. Use 'met', 'aic', 'cma', or 'europeana'."
        )

        resp = client.get("/catalog/search?source=foo&q=test")
        assert resp.status_code == 400

    @patch("frameart.api.public_domain.search_artworks")
    def test_drops_invalid_items_instead_of_500(self, mock_search):
        mock_search.return_value = [
            {"source": "met", "artwork_id": "123"},  # missing required fields
            {
                "source": "met",
                "artwork_id": "456",
                "title": "Valid",
                "image_url": "https://example.com/full.jpg",
                "is_public_domain": True,
            },
        ]

        resp = client.get("/catalog/search?source=met&q=valid")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["artwork_id"] == "456"


class TestCatalogApply:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_import_and_apply")
    @patch("frameart.api.public_domain.download_artwork_image")
    def test_apply_public_artwork_success(self, mock_download, mock_run, mock_settings):
        settings = MagicMock()
        settings.data_dir = Path("/tmp/frameart_test")
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_download.return_value = (
            Path("/tmp/frameart_test/catalog_cache/met_123.jpg"),
            {
                "source": "met",
                "artwork_id": "123",
                "title": "Water Lilies",
                "image_url": "https://example.com/full.jpg",
                "is_public_domain": True,
            },
        )
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/catalog/apply",
            json={"source": "met", "artwork_id": "123", "tv_ip": "192.168.1.100"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content_id"] == "MY_ART_001"
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["source_metadata"]["artwork_id"] == "123"

    @patch("frameart.api._settings")
    @patch("frameart.api.public_domain.download_artwork_image")
    def test_apply_public_artwork_bad_input_returns_400(self, mock_download, mock_settings):
        settings = MagicMock()
        settings.data_dir = Path("/tmp/frameart_test")
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_download.side_effect = ValueError("Artwork is unavailable or not public domain.")

        resp = client.post(
            "/catalog/apply",
            json={"source": "met", "artwork_id": "123", "tv_ip": "192.168.1.100"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/apply
# ---------------------------------------------------------------------------

class TestJobApply:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_apply")
    def test_success(self, mock_run, mock_settings):
        import tempfile

        settings = MagicMock()
        # Create a temp file to simulate the artifact
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts = Path(tmpdir) / "artifacts" / "2025" / "01" / "01" / "test-job"
            artifacts.mkdir(parents=True)
            (artifacts / "final.png").write_bytes(b"fakepng")
            settings.data_dir = Path(tmpdir)
            mock_settings.return_value = settings
            mock_run.return_value = _fake_result()

            resp = client.post(
                "/jobs/test-job/apply",
                json={"tv_ip": "192.168.1.100", "matte": "shadowbox_polar"},
            )
            assert resp.status_code == 200
            assert resp.json()["content_id"] == "MY_ART_001"

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_apply")
    @patch("frameart.tv.controller.switch_art")
    @patch("frameart.tv.controller.list_art_deduplicated")
    def test_reuses_existing_tv_content_without_upload(
        self,
        mock_list_art,
        mock_switch_art,
        mock_run_apply,
        mock_settings,
    ):
        import tempfile

        settings = MagicMock()
        settings.tvs = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "artifacts" / "2025" / "01" / "01" / "test-job"
            job_dir.mkdir(parents=True)
            (job_dir / "final.png").write_bytes(b"fakepng")
            (job_dir / "meta.json").write_text('{"job_id":"test-job","content_id":"MY_F1234"}')
            settings.data_dir = Path(tmpdir)
            mock_settings.return_value = settings

            mock_list_art.return_value = [{"content_id": "MY_F1234", "is_favourite": False}]
            mock_switch_art.return_value = True

            resp = client.post(
                "/jobs/test-job/apply",
                json={"tv_ip": "192.168.1.100", "matte": "none"},
            )

            assert resp.status_code == 200
            data = resp.json()
            assert data["content_id"] == "MY_F1234"
            assert data["tv_switched"] is True
            assert data["metadata"]["reused_existing_content"] is True
            mock_run_apply.assert_not_called()

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_apply")
    def test_persists_content_id_after_apply(self, mock_run, mock_settings):
        import json
        import tempfile

        settings = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "artifacts" / "2025" / "01" / "01" / "test-job"
            job_dir.mkdir(parents=True)
            (job_dir / "final.png").write_bytes(b"fakepng")
            (job_dir / "meta.json").write_text('{"job_id":"test-job"}')
            settings.data_dir = Path(tmpdir)
            mock_settings.return_value = settings
            mock_run.return_value = _fake_result(
                content_id="MY_F9000",
                metadata={"tv_ip": "192.168.1.100"},
            )

            resp = client.post(
                "/jobs/test-job/apply",
                json={"tv_ip": "192.168.1.100", "matte": "none"},
            )
            assert resp.status_code == 200

            persisted = json.loads((job_dir / "meta.json").read_text())
            assert persisted["content_id"] == "MY_F9000"
            assert persisted["tv_content_ids"]["192.168.1.100"] == "MY_F9000"

    @patch("frameart.api._settings")
    def test_not_found(self, mock_settings):
        settings = MagicMock()
        settings.data_dir = Path("/tmp/nonexistent_frameart_test")
        mock_settings.return_value = settings

        resp = client.post(
            "/jobs/nonexistent/apply",
            json={"tv_ip": "192.168.1.100"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /async/generate + GET /jobs/{id}/status
# ---------------------------------------------------------------------------

class TestAsyncGenerate:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate")
    def test_submit_and_poll(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        # Submit
        resp = client.post("/async/generate", json={"prompt": "a sunset"})
        assert resp.status_code == 200
        data = resp.json()
        assert "job_id" in data
        assert data["status"] == "pending"

        job_id = data["job_id"]

        # Wait for the background thread to complete
        for _ in range(50):
            status_resp = client.get(f"/jobs/{job_id}/status")
            if status_resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        status_data = status_resp.json()
        assert status_data["status"] == "completed"
        assert status_data["result"]["job_id"] == "120000-abcd1234"
        assert status_data["error"] is None

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate")
    def test_failed_job(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result(error="provider exploded")

        resp = client.post("/async/generate", json={"prompt": "fail"})
        job_id = resp.json()["job_id"]

        for _ in range(50):
            status_resp = client.get(f"/jobs/{job_id}/status")
            if status_resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        status_data = status_resp.json()
        assert status_data["status"] == "failed"
        assert "provider exploded" in status_data["error"]


class TestAsyncGenerateAndApply:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate_and_apply")
    def test_submit(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/async/generate-and-apply",
            json={"prompt": "mountains", "tv_ip": "10.0.0.1"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"


class TestAsyncApply:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_apply")
    def test_submit(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/async/apply",
            json={"image_path": "/tmp/test.png", "tv_ip": "10.0.0.1"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# GET /jobs/{id}/status — not found
# ---------------------------------------------------------------------------

class TestJobStatusNotFound:
    def test_missing_job(self):
        resp = client.get("/jobs/nonexistent/status")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET / — Web UI
# ---------------------------------------------------------------------------

class TestWebUI:
    def test_returns_html(self):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "FrameArt" in resp.text
