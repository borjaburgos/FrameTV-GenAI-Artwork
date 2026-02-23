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
    meural_displayed: bool = False
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

class TestTVCleanup:
    @patch("frameart.tv.cleanup.cleanup_artworks")
    @patch("frameart.api.load_settings")
    def test_cleanup_success(self, mock_settings, mock_cleanup):
        from frameart.tv.cleanup import CleanupResult

        mock_settings.return_value = MagicMock(tvs={})
        mock_cleanup.return_value = CleanupResult(
            deleted=["MY_F0001", "MY_F0002"], kept=3, skipped_favourites=1,
        )
        resp = client.post("/tv/cleanup", json={"tv_ip": "1.2.3.4", "keep": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == ["MY_F0001", "MY_F0002"]
        assert data["kept"] == 3
        assert data["skipped_favourites"] == 1

    @patch("frameart.api.load_settings")
    def test_cleanup_no_tv_returns_400(self, mock_settings):
        mock_settings.return_value = MagicMock(tvs={})
        resp = client.post("/tv/cleanup", json={"keep": 5})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /meural/status
# ---------------------------------------------------------------------------

class TestMeuralStatus:
    @patch("frameart.api._settings")
    @patch("frameart.meural.controller.get_status")
    def test_with_meural_ip(self, mock_status, mock_settings):
        settings = MagicMock()
        settings.meurals = {}
        mock_settings.return_value = settings

        mock_status.return_value = MagicMock(
            reachable=True,
            sleeping=False,
            orientation="vertical",
            current_gallery="5",
            current_item="42",
            device_name="Office",
            device_model="MC327",
            error=None,
        )

        resp = client.get("/meural/status?meural_ip=192.168.1.50")
        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is True
        assert data["orientation"] == "vertical"

    @patch("frameart.api._settings")
    def test_no_meural_returns_400(self, mock_settings):
        settings = MagicMock()
        settings.meurals = {}
        mock_settings.return_value = settings

        resp = client.get("/meural/status")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /meural/display
# ---------------------------------------------------------------------------

class TestMeuralDisplay:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_meural_apply")
    def test_success(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result(meural_displayed=True, tv_switched=False)

        resp = client.post(
            "/meural/display",
            json={"image_path": "/tmp/test.png", "meural_ip": "192.168.1.50"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["meural_displayed"] is True


# ---------------------------------------------------------------------------
# POST /meural/generate-and-display
# ---------------------------------------------------------------------------

class TestMeuralGenerateAndDisplay:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_meural_generate_and_apply")
    def test_success(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result(meural_displayed=True, tv_switched=False)

        resp = client.post(
            "/meural/generate-and-display",
            json={
                "prompt": "a sunset",
                "meural_ip": "192.168.1.50",
                "orientation": "vertical",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["meural_displayed"] is True
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# GET / — Web UI
# ---------------------------------------------------------------------------

class TestWebUI:
    def test_returns_html(self):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "FrameArt" in resp.text
