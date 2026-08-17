"""Tests for the FrameArt HTTP API."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

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


def _jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 4), "blue").save(output, format="JPEG")
    return output.getvalue()


@pytest.fixture
def managed_config_env(tmp_path, monkeypatch):
    """Isolate web-managed configuration beneath a temporary data directory."""
    config_file = tmp_path / "base.yaml"
    config_file.write_text(
        "auth_enabled: false\n"
        "default_provider: openai\n"
        "providers:\n"
        "  openai:\n"
        "    model: dall-e-3\n"
    )
    monkeypatch.setenv("FRAMEART_CONFIG", str(config_file))
    monkeypatch.setenv("FRAMEART_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "false")
    return tmp_path


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

    def test_readiness_checks_local_storage(self, managed_config_env):
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert {check["name"] for check in data["checks"]} >= {
            "data_directory",
            "settings_store",
            "disk_space",
        }


class TestDiagnosticsAndBackups:
    def test_diagnostics_and_support_bundle_never_return_provider_key(
        self,
        managed_config_env,
    ):
        created = client.put(
            "/settings/providers/openai",
            json={"model": "gpt-image-1", "api_key": "never-return-this-key"},
        )
        assert created.status_code == 200

        diagnostics = client.get("/settings/diagnostics")
        assert diagnostics.status_code == 200
        assert "never-return-this-key" not in diagnostics.text
        assert diagnostics.json()["configuration"]["provider_key_sources"]["openai"] == "managed"

        support = client.get("/settings/diagnostics/support-bundle")
        assert support.status_code == 200
        assert "attachment" in support.headers["content-disposition"]
        assert "never-return-this-key" not in support.text

    def test_export_import_and_restore_round_trip(self, managed_config_env):
        update = client.put(
            "/settings/providers/openai",
            json={"model": "gpt-image-1", "api_key": "preserved-secret-key"},
        )
        assert update.status_code == 200
        exported = client.get("/settings/export")
        assert exported.status_code == 200
        payload = exported.json()
        assert "preserved-secret-key" not in exported.text

        backup = client.post("/settings/backups")
        assert backup.status_code == 201
        backup_id = backup.json()["backup_id"]
        assert client.get("/settings/backups").json()["backups"]

        payload["settings"]["default_model"] = "imported-model"
        imported = client.post("/settings/import", json=payload)
        assert imported.status_code == 200
        assert client.get("/settings/providers").json()["default_model"] == "imported-model"
        assert client.get("/settings/providers").json()["providers"][0]["has_api_key"] is True

        restored = client.post(f"/settings/backups/{backup_id}/restore")
        assert restored.status_code == 200
        assert client.get("/settings/providers").json()["default_model"] is None

    def test_import_rejects_secret_fields(self, managed_config_env):
        response = client.post(
            "/settings/import",
            json={
                "schema_version": 1,
                "settings": {"providers": {"openai": {"api_key": "not-allowed"}}},
            },
        )
        assert response.status_code == 400


class TestAuthentication:
    def test_admin_token_creates_browser_session(self, monkeypatch):
        token = "admin-token-with-at-least-twenty-characters"
        monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "true")
        monkeypatch.setenv("FRAMEART_ADMIN_TOKEN", token)

        with TestClient(app) as secured_client:
            assert secured_client.get("/health").status_code == 200
            assert secured_client.get("/static/app.css").status_code == 200
            assert secured_client.get("/styles").status_code == 401

            login = secured_client.post("/auth/session", json={"token": token})
            assert login.status_code == 200
            assert login.json()["scopes"] == ["admin", "control", "read"]
            assert secured_client.get("/styles").status_code == 200

    def test_automation_token_cannot_use_admin_scope(self, monkeypatch):
        token = "automation-token-with-twenty-characters"
        monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "true")
        monkeypatch.setenv("FRAMEART_ADMIN_TOKEN", "admin-token-with-twenty-characters")
        monkeypatch.setenv("FRAMEART_AUTOMATION_TOKEN", token)
        headers = {"Authorization": f"Bearer {token}"}

        with TestClient(app) as secured_client:
            assert secured_client.get("/styles", headers=headers).status_code == 200
            denied = secured_client.post(
                "/jobs/delete",
                json={"job_ids": ["job-1"]},
                headers=headers,
            )
            assert denied.status_code == 403

    def test_automation_token_cannot_read_managed_settings(self, monkeypatch):
        token = "automation-token-with-twenty-characters"
        monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "true")
        monkeypatch.setenv("FRAMEART_ADMIN_TOKEN", "admin-token-with-twenty-characters")
        monkeypatch.setenv("FRAMEART_AUTOMATION_TOKEN", token)
        headers = {"Authorization": f"Bearer {token}"}

        with TestClient(app) as secured_client:
            denied = secured_client.get("/settings/providers", headers=headers)
            assert denied.status_code == 403


class TestServerSecurity:
    @patch("uvicorn.run")
    def test_loopback_server_uses_one_worker(self, mock_run, monkeypatch):
        from frameart.api import run_server

        monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "false")
        run_server(host="127.0.0.1", port=8123)
        assert mock_run.call_args.kwargs["workers"] == 1

    @patch("uvicorn.run")
    def test_lan_bind_requires_authentication(self, mock_run, monkeypatch):
        from frameart.api import run_server

        monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "false")
        with pytest.raises(RuntimeError, match="non-loopback"):
            run_server(host="0.0.0.0", port=8123)
        mock_run.assert_not_called()


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
# /providers
# ---------------------------------------------------------------------------

class TestProviders:
    @patch("frameart.api._settings")
    def test_list_configured_providers_and_models(self, mock_settings):
        settings = MagicMock()
        settings.default_provider = "openai"
        settings.default_model = "gpt-image-1"
        settings.providers = {
            "openai": MagicMock(model="dall-e-3"),
            "ollama": MagicMock(model="sdxl"),
        }
        mock_settings.return_value = settings

        resp = client.get("/providers")
        assert resp.status_code == 200
        data = resp.json()

        assert data["default_provider"] == "openai"
        names = [p["name"] for p in data["providers"]]
        assert names == ["ollama", "openai"]

        openai = next(p for p in data["providers"] if p["name"] == "openai")
        assert openai["is_default"] is True
        assert openai["default_model"] == "gpt-image-1"
        assert openai["models"] == ["dall-e-3", "gpt-image-1"]

        ollama = next(p for p in data["providers"] if p["name"] == "ollama")
        assert ollama["is_default"] is False
        assert ollama["default_model"] == "sdxl"
        assert ollama["models"] == ["sdxl"]

    @patch("frameart.api._settings")
    def test_default_provider_included_even_without_provider_block(self, mock_settings):
        settings = MagicMock()
        settings.default_provider = "openai"
        settings.default_model = None
        settings.providers = {}
        mock_settings.return_value = settings

        resp = client.get("/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["default_provider"] == "openai"
        assert data["providers"] == [
            {
                "name": "openai",
                "is_default": True,
                "models": [],
                "default_model": None,
            }
        ]

    @patch("frameart.api._settings")
    @patch("frameart.api._fetch_openai_image_models")
    def test_openai_live_models_are_merged(self, mock_live_models, mock_settings):
        settings = MagicMock()
        settings.default_provider = "openai"
        settings.default_model = "gpt-image-1"
        settings.providers = {
            "openai": MagicMock(model="dall-e-3"),
            "ollama": MagicMock(model="sdxl"),
        }
        mock_settings.return_value = settings
        mock_live_models.return_value = ["gpt-image-1", "gpt-image-2"]

        resp = client.get("/providers")
        assert resp.status_code == 200
        data = resp.json()

        openai = next(p for p in data["providers"] if p["name"] == "openai")
        assert openai["models"] == ["dall-e-3", "gpt-image-1", "gpt-image-2"]
        mock_live_models.assert_called_once()

    @patch("frameart.api._settings")
    def test_extra_models_are_included(self, mock_settings):
        settings = MagicMock()
        settings.default_provider = "google"
        settings.default_model = "nano-banana-2"
        settings.providers = {
            "google": MagicMock(
                model="nano-banana",
                extra={
                    "models": [
                        "nano-banana",
                        "nano-banana-2",
                        "gemini-2.5-flash-image-preview",
                        "gemini-2.5-pro",
                    ]
                },
            ),
        }
        mock_settings.return_value = settings

        resp = client.get("/providers")
        assert resp.status_code == 200
        data = resp.json()
        google = next(p for p in data["providers"] if p["name"] == "google")
        assert google["models"] == [
            "nano-banana",
            "nano-banana-2",
            "gemini-2.5-flash-image-preview",
        ]


class TestManagedProviders:
    def test_update_key_is_persisted_but_never_returned(self, managed_config_env):
        resp = client.put(
            "/settings/providers/openai",
            json={
                "model": "gpt-image-1",
                "timeout": 90,
                "models": ["gpt-image-1", "dall-e-3"],
                "api_key": "top-secret-api-key",
            },
        )

        assert resp.status_code == 200
        assert "top-secret-api-key" not in resp.text
        provider = resp.json()["providers"][0]
        assert provider["has_api_key"] is True
        assert provider["api_key_source"] == "managed"
        assert provider["model"] == "gpt-image-1"

        from frameart.config import load_settings

        settings = load_settings()
        assert settings.providers["openai"].api_key == "top-secret-api-key"

    def test_create_change_default_and_delete_provider(self, managed_config_env):
        created = client.post(
            "/settings/providers",
            json={
                "name": "ollama",
                "base_url": "http://host.docker.internal:11434",
                "model": "sdxl",
                "timeout": 300,
            },
        )
        assert created.status_code == 201
        assert {p["name"] for p in created.json()["providers"]} == {"openai", "ollama"}

        defaults = client.put(
            "/settings/defaults",
            json={"provider": "ollama", "model": "sdxl"},
        )
        assert defaults.status_code == 200
        assert defaults.json()["default_provider"] == "ollama"

        deleted = client.delete("/settings/providers/openai")
        assert deleted.status_code == 200
        assert [p["name"] for p in deleted.json()["providers"]] == ["ollama"]

    def test_cannot_delete_default_provider(self, managed_config_env):
        resp = client.delete("/settings/providers/openai")
        assert resp.status_code == 409

    @patch("frameart.api.httpx.Client")
    def test_provider_connection(self, mock_client_cls, managed_config_env):
        client.put(
            "/settings/providers/openai",
            json={"model": "gpt-image-1", "api_key": "test-key"},
        )
        response = MagicMock()
        response.raise_for_status.return_value = None
        mock_client_cls.return_value.__enter__.return_value.get.return_value = response

        resp = client.post("/settings/providers/openai/test")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "detail": "Connected to openai."}

    @patch("frameart.api._settings")
    @patch("frameart.api._fetch_google_image_models")
    def test_google_live_models_are_merged(self, mock_google_models, mock_settings):
        settings = MagicMock()
        settings.default_provider = "google"
        settings.default_model = None
        settings.providers = {
            "google": MagicMock(
                model="gemini-2.5-flash-image-preview",
                extra={"models": ["nano-banana-2", "gemini-2.5-pro"]},
            ),
        }
        mock_settings.return_value = settings
        mock_google_models.return_value = [
            "gemini-2.0-flash-exp-image-generation",
            "nano-banana-2",
            "gemini-2.5-flash",
        ]

        resp = client.get("/providers")
        assert resp.status_code == 200
        data = resp.json()
        google = next(p for p in data["providers"] if p["name"] == "google")
        assert google["models"] == [
            "gemini-2.5-flash-image-preview",
            "nano-banana-2",
            "gemini-2.0-flash-exp-image-generation",
        ]
        mock_google_models.assert_called_once()

    @patch("frameart.api._settings")
    @patch("frameart.api._fetch_openai_image_models")
    def test_openai_non_image_models_are_filtered(self, mock_openai_models, mock_settings):
        settings = MagicMock()
        settings.default_provider = "openai"
        settings.default_model = "gpt-4o"
        settings.providers = {
            "openai": MagicMock(
                model="gpt-4o-mini",
                extra={"models": ["dall-e-3", "gpt-4o", "gpt-image-1.5"]},
            ),
        }
        mock_settings.return_value = settings
        mock_openai_models.return_value = ["gpt-image-1.5", "gpt-4o", "dall-e-2"]

        resp = client.get("/providers")
        assert resp.status_code == 200
        data = resp.json()
        openai = next(p for p in data["providers"] if p["name"] == "openai")
        assert openai["models"] == [
            "dall-e-3",
            "gpt-image-1.5",
            "dall-e-2",
        ]
        assert openai["default_model"] is None


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

    def test_blank_prompt_returns_422(self):
        resp = client.post("/generate", json={"prompt": "   "})
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

    def test_rejects_public_tv_ip(self):
        resp = client.post(
            "/generate-and-apply",
            json={"prompt": "flowers", "tv_ip": "8.8.8.8"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /apply
# ---------------------------------------------------------------------------

class TestApply:
    def test_server_path_endpoint_is_removed(self):
        resp = client.post(
            "/apply",
            json={"image_path": "/tmp/test.png", "tv_ip": "192.168.1.50"},
        )
        assert resp.status_code == 404
        assert "/apply" not in client.get("/openapi.json").json()["paths"]


# ---------------------------------------------------------------------------
# POST /upload-and-apply
# ---------------------------------------------------------------------------

class TestUploadAndApply:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_import_and_apply")
    def test_success(self, mock_run, mock_settings, tmp_path):
        settings = MagicMock()
        settings.data_dir = tmp_path
        mock_settings.return_value = settings
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/upload-and-apply",
            data={"tv_ip": "192.168.1.50", "matte": "none"},
            files={"image": ("sample.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content_id"] == "MY_ART_001"
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["tv_ip"] == "192.168.1.50"

    @patch("frameart.api._settings")
    def test_rejects_unsupported_file_extension(self, mock_settings, tmp_path):
        settings = MagicMock()
        settings.data_dir = tmp_path
        mock_settings.return_value = settings

        resp = client.post(
            "/upload-and-apply",
            data={"tv_ip": "192.168.1.50"},
            files={"image": ("sample.gif", b"GIF89a", "image/gif")},
        )
        assert resp.status_code == 400
        assert "Unsupported file type" in resp.json()["detail"]

    def test_rejects_corrupt_image_content(self):
        resp = client.post(
            "/upload-and-apply",
            data={"tv_ip": "192.168.1.50"},
            files={"image": ("sample.jpg", b"not-a-real-jpeg", "image/jpeg")},
        )
        assert resp.status_code == 400
        assert "not a valid image" in resp.json()["detail"]


class TestEditAndApply:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_edit_and_apply")
    def test_success(self, mock_run, mock_settings, tmp_path):
        settings = MagicMock()
        settings.data_dir = tmp_path
        mock_settings.return_value = settings
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/edit-and-apply",
            data={
                "prompt": "an impressionist rendition of this family picture",
                "provider": "openai",
                "model": "gpt-image-1",
                "tv_ip": "192.168.1.50",
            },
            files={"image": ("sample.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content_id"] == "MY_ART_001"
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["provider_name"] == "openai"
        assert mock_run.call_args.kwargs["model"] == "gpt-image-1"
        assert mock_run.call_args.kwargs["tv_ip"] == "192.168.1.50"

    @patch("frameart.api._settings")
    def test_requires_prompt(self, mock_settings, tmp_path):
        settings = MagicMock()
        settings.data_dir = tmp_path
        mock_settings.return_value = settings

        resp = client.post(
            "/edit-and-apply",
            data={"prompt": "   ", "tv_ip": "192.168.1.50"},
            files={"image": ("sample.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 400
        assert "Edit prompt cannot be empty" in resp.json()["detail"]

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_edit_and_apply")
    def test_no_upload_allows_missing_tv(self, mock_run, mock_settings, tmp_path):
        settings = MagicMock()
        settings.data_dir = tmp_path
        mock_settings.return_value = settings
        mock_run.return_value = _fake_result(content_id=None, tv_switched=False)

        resp = client.post(
            "/edit-and-apply",
            data={
                "prompt": "impressionist rendition",
                "provider": "openai",
                "no_upload": "true",
            },
            files={"image": ("sample.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 200
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["no_upload"] is True
        assert mock_run.call_args.kwargs["tv_ip"] is None


class TestEditFromExistingArtwork:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_edit_and_apply")
    def test_edit_from_job_success(self, mock_run, mock_settings):
        import tempfile

        settings = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts = Path(tmpdir) / "artifacts" / "2026" / "02" / "28" / "test-job"
            artifacts.mkdir(parents=True)
            (artifacts / "final.png").write_bytes(b"fakepng")
            settings.data_dir = Path(tmpdir)
            mock_settings.return_value = settings
            mock_run.return_value = _fake_result(content_id=None, tv_switched=False)

            resp = client.post(
                "/jobs/test-job/edit-and-apply",
                json={
                    "prompt": "create a colorful variation",
                    "provider": "openai",
                    "model": "gpt-image-1",
                    "no_upload": True,
                },
            )
            assert resp.status_code == 200
            mock_run.assert_called_once()
            assert mock_run.call_args.args[1].endswith("final.png")
            assert mock_run.call_args.args[2] == "create a colorful variation"
            assert mock_run.call_args.kwargs["provider_name"] == "openai"
            assert mock_run.call_args.kwargs["model"] == "gpt-image-1"
            assert mock_run.call_args.kwargs["no_upload"] is True

    @patch("frameart.api._settings")
    def test_edit_from_job_not_found(self, mock_settings):
        settings = MagicMock()
        settings.data_dir = Path("/tmp/nonexistent_frameart_test")
        mock_settings.return_value = settings

        resp = client.post(
            "/jobs/nonexistent/edit-and-apply",
            json={"prompt": "variation"},
        )
        assert resp.status_code == 404

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_edit_and_apply")
    @patch("frameart.tv.controller.get_art_thumbnail")
    def test_edit_from_tv_art_success(
        self,
        mock_thumbnail,
        mock_run,
        mock_settings,
        tmp_path,
    ):
        settings = MagicMock()
        settings.data_dir = tmp_path
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_thumbnail.return_value = b"fake-jpeg-bytes"
        mock_run.return_value = _fake_result(content_id=None, tv_switched=False)

        resp = client.post(
            "/tv/art/edit-and-apply",
            json={
                "content_id": "MY_F0001",
                "source_tv_ip": "192.168.1.50",
                "prompt": "turn this into impressionist style",
                "no_upload": True,
            },
        )
        assert resp.status_code == 200
        mock_thumbnail.assert_called_once()
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["no_upload"] is True

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_edit_and_apply")
    @patch("frameart.tv.controller.get_art_thumbnail")
    def test_edit_from_tv_art_prefers_local_artifact_over_thumbnail(
        self,
        mock_thumbnail,
        mock_run,
        mock_settings,
    ):
        import tempfile

        settings = MagicMock()
        settings.tvs = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "artifacts" / "2026" / "03" / "02" / "job-1"
            job_dir.mkdir(parents=True)
            (job_dir / "final.png").write_bytes(b"fake-local-png")
            (job_dir / "meta.json").write_text(
                '{"job_id":"job-1","content_id":"MY_F0001","tv_ip":"192.168.1.50"}'
            )
            settings.data_dir = Path(tmpdir)
            mock_settings.return_value = settings
            mock_thumbnail.return_value = b"fake-thumb"
            mock_run.return_value = _fake_result(content_id=None, tv_switched=False)

            resp = client.post(
                "/tv/art/edit-and-apply",
                json={
                    "content_id": "MY_F0001",
                    "source_tv_ip": "192.168.1.50",
                    "prompt": "turn this into impressionist style",
                    "no_upload": True,
                },
            )

            assert resp.status_code == 200
            mock_thumbnail.assert_not_called()
            mock_run.assert_called_once()
            assert mock_run.call_args.args[1].endswith("final.png")

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_edit_and_apply")
    @patch("frameart.tv.controller.get_art_thumbnail")
    def test_edit_from_tv_defaults_target_to_source_tv(
        self,
        mock_thumbnail,
        mock_run,
        mock_settings,
        tmp_path,
    ):
        settings = MagicMock()
        settings.data_dir = tmp_path
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_thumbnail.return_value = b"fake-jpeg-bytes"
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/tv/art/edit-and-apply",
            json={
                "content_id": "MY_F0001",
                "source_tv_ip": "192.168.1.77",
                "prompt": "make a surreal variant",
            },
        )
        assert resp.status_code == 200
        assert mock_run.call_args.kwargs["tv_ip"] == "192.168.1.77"

    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.get_art_thumbnail")
    def test_edit_from_tv_thumbnail_missing_returns_404(
        self,
        mock_thumbnail,
        mock_settings,
        tmp_path,
    ):
        settings = MagicMock()
        settings.data_dir = tmp_path
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_thumbnail.return_value = None

        resp = client.post(
            "/tv/art/edit-and-apply",
            json={
                "content_id": "MY_F0001",
                "source_tv_ip": "192.168.1.50",
                "prompt": "variation",
                "no_upload": True,
            },
        )
        assert resp.status_code == 404


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

    def test_rejects_glob_metacharacters(self):
        resp = client.get("/jobs/%2A/image")
        assert resp.status_code == 400

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

    @patch("frameart.tv.discovery.discover")
    def test_reports_unavailable_discovery(self, mock_discover):
        from frameart.tv.discovery import SSDPDiscoveryError

        mock_discover.side_effect = SSDPDiscoveryError("Use the LAN deployment")

        resp = client.get("/tv/discover")

        assert resp.status_code == 503
        assert resp.json() == {"detail": "Use the LAN deployment"}


class TestManagedTVs:
    def test_create_update_and_delete_tv(self, managed_config_env):
        created = client.post(
            "/settings/tvs",
            json={
                "profile_id": "living_room",
                "ip": "192.168.50.25",
                "port": 8002,
                "client_name": "FrameArt Living Room",
                "ssl": True,
            },
        )
        assert created.status_code == 201
        assert created.json()["tvs"] == [
            {
                "profile_id": "living_room",
                "ip": "192.168.50.25",
                "port": 8002,
                "client_name": "FrameArt Living Room",
                "ssl": True,
                "token_configured": False,
            }
        ]

        updated = client.put(
            "/settings/tvs/living_room",
            json={
                "ip": "10.0.0.25",
                "port": 8001,
                "client_name": "Living Room",
                "ssl": False,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["tvs"][0]["ip"] == "10.0.0.25"
        assert updated.json()["tvs"][0]["ssl"] is False

        deleted = client.delete("/settings/tvs/living_room")
        assert deleted.status_code == 200
        assert deleted.json() == {"tvs": []}

    def test_rejects_public_tv_ip(self, managed_config_env):
        resp = client.post(
            "/settings/tvs",
            json={"profile_id": "invalid", "ip": "8.8.8.8"},
        )
        assert resp.status_code == 422

    @patch("frameart.tv.controller.get_status")
    def test_tv_connection(self, mock_status, managed_config_env):
        client.post(
            "/settings/tvs",
            json={"profile_id": "living_room", "ip": "192.168.50.25"},
        )
        mock_status.return_value = MagicMock(
            reachable=True,
            art_mode_supported=True,
            error=None,
        )

        resp = client.get("/settings/tvs/living_room/test")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("frameart.tv.controller._run_with_timeout")
    def test_tv_pairing(self, mock_run, managed_config_env):
        client.post(
            "/settings/tvs",
            json={"profile_id": "living_room", "ip": "192.168.50.25"},
        )
        mock_run.return_value = (True, None)

        resp = client.post("/settings/tvs/living_room/pair")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True


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

    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.list_art_deduplicated")
    def test_upstream_timeout_returns_502(self, mock_list, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_list.side_effect = TimeoutError("timed out")

        resp = client.get("/tv/art?tv_ip=192.168.1.100")
        assert resp.status_code == 502
        assert "TV art list failed" in resp.json()["detail"]


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
    @patch("frameart.tv.controller.delete_art")
    @patch("frameart.tv.controller.list_art_deduplicated")
    def test_favorite_lookup_failure_cancels_delete(
        self,
        mock_list,
        mock_delete,
        mock_settings,
    ):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_list.side_effect = TimeoutError("TV did not respond")

        resp = client.post(
            "/tv/art/delete",
            json={"content_ids": ["MY_F0001"], "tv_ip": "192.168.1.100"},
        )

        assert resp.status_code == 502
        assert "deletion was cancelled" in resp.json()["detail"]
        mock_delete.assert_not_called()

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
    def test_success(self, mock_switch, mock_settings, tmp_path):
        settings = MagicMock()
        settings.tvs = {}
        settings.data_dir = tmp_path
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

    @patch("frameart.api._settings")
    @patch("frameart.tv.controller.get_matte_list")
    def test_upstream_failure_returns_502(self, mock_mattes, mock_settings):
        settings = MagicMock()
        settings.tvs = {}
        mock_settings.return_value = settings
        mock_mattes.side_effect = TimeoutError("timed out")

        resp = client.get("/tv/mattes?tv_ip=192.168.1.100")
        assert resp.status_code == 502
        assert "TV matte list failed" in resp.json()["detail"]


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
    def test_request_metadata_contains_provider_and_model(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/async/generate",
            json={"prompt": "a sunset", "provider": "openai", "model": "gpt-image-1"},
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        for _ in range(50):
            status_resp = client.get(f"/jobs/{job_id}/status")
            if status_resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        request_summary = status_resp.json()["request"]
        assert request_summary["provider"] == "openai"
        assert request_summary["model"] == "gpt-image-1"

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

    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate_and_apply")
    def test_request_metadata_contains_provider_model_and_tv(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        resp = client.post(
            "/async/generate-and-apply",
            json={
                "prompt": "mountains",
                "provider": "openai",
                "model": "gpt-image-1",
                "tv_ip": "10.0.0.1",
            },
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        for _ in range(50):
            status_resp = client.get(f"/jobs/{job_id}/status")
            if status_resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        request_summary = status_resp.json()["request"]
        assert request_summary["provider"] == "openai"
        assert request_summary["model"] == "gpt-image-1"
        assert request_summary["tv_ip"] == "10.0.0.1"


class TestAsyncApply:
    def test_server_path_endpoint_is_removed(self):
        resp = client.post(
            "/async/apply",
            json={"image_path": "/tmp/test.png", "tv_ip": "10.0.0.1"},
        )
        assert resp.status_code == 404
        assert "/async/apply" not in client.get("/openapi.json").json()["paths"]


# ---------------------------------------------------------------------------
# GET /jobs/{id}/status — not found
# ---------------------------------------------------------------------------

class TestJobStatusNotFound:
    def test_missing_job(self):
        resp = client.get("/jobs/nonexistent/status")
        assert resp.status_code == 404


class TestAsyncJobsList:
    @patch("frameart.api._settings")
    @patch("frameart.pipeline.run_generate")
    def test_lists_recent_async_jobs(self, mock_run, mock_settings):
        mock_settings.return_value = MagicMock()
        mock_run.return_value = _fake_result()

        submit = client.post(
            "/async/generate",
            json={"prompt": "list me", "provider": "openai", "model": "gpt-image-1"},
        )
        assert submit.status_code == 200
        job_id = submit.json()["job_id"]

        for _ in range(50):
            status_resp = client.get(f"/jobs/{job_id}/status")
            if status_resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        resp = client.get("/async/jobs?limit=20")
        assert resp.status_code == 200
        jobs = resp.json()
        found = next((j for j in jobs if j["job_id"] == job_id), None)
        assert found is not None
        assert found["request"]["type"] == "generate"
        assert found["request"]["provider"] == "openai"
        assert found["request"]["model"] == "gpt-image-1"


class TestLibraryManagement:
    @staticmethod
    def create_artifact(data_dir: Path, job_id: str, prompt: str, provider: str = "openai"):
        job_dir = data_dir / "artifacts" / "2026" / "01" / "01" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "final.png").write_bytes(_jpeg_bytes())
        (job_dir / "meta.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "prompt_original": prompt,
                    "provider": provider,
                }
            )
        )

    def test_search_tags_and_collections(self, managed_config_env):
        self.create_artifact(managed_config_env, "library-one", "Blue mountain lake")
        self.create_artifact(managed_config_env, "library-two", "Red city skyline", "ollama")

        tagged = client.put("/jobs/library-one/tags", json={"tags": ["Travel", "blue"]})
        assert tagged.status_code == 200
        collection = client.post("/library/collections", json={"name": "Favorites"})
        assert collection.status_code == 201
        collection_id = collection.json()["id"]
        added = client.post(
            f"/library/collections/{collection_id}/items",
            json={"job_ids": ["library-one"]},
        )
        assert added.status_code == 200

        by_text = client.get("/jobs", params={"q": "mountain"}).json()
        assert [item["job_id"] for item in by_text] == ["library-one"]
        assert by_text[0]["tags"] == ["blue", "travel"]
        assert by_text[0]["collections"] == ["Favorites"]
        assert [item["job_id"] for item in client.get("/jobs?tag=travel").json()] == [
            "library-one"
        ]
        assert [
            item["job_id"]
            for item in client.get(f"/jobs?collection={collection_id}").json()
        ] == ["library-one"]

        removed = client.request(
            "DELETE",
            f"/library/collections/{collection_id}/items",
            json={"job_ids": ["library-one"]},
        )
        assert removed.status_code == 200
        assert client.delete(f"/library/collections/{collection_id}").status_code == 200

    def test_display_history_endpoint(self, managed_config_env):
        from frameart.library import LibraryStore

        LibraryStore(managed_config_env).record_display(
            job_id="library-one",
            content_id="content-one",
            tv_target="living-room",
            source="library-upload",
        )

        history = client.get("/library/history").json()
        assert history[0]["job_id"] == "library-one"
        assert history[0]["tv_target"] == "living-room"


class TestAutomationManagement:
    @staticmethod
    def create_artifact(data_dir: Path, job_id: str):
        job_dir = data_dir / "artifacts" / "2026" / "01" / "01" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "final.png").write_bytes(_jpeg_bytes())
        (job_dir / "meta.json").write_text(
            json.dumps({"job_id": job_id, "prompt_original": "Scheduled landscape"})
        )

    def test_group_playlist_and_schedule_crud(self, managed_config_env):
        self.create_artifact(managed_config_env, "scheduled-one")
        tv = client.post(
            "/settings/tvs",
            json={
                "profile_id": "living_room",
                "ip": "192.168.1.50",
                "port": 8002,
                "client_name": "FrameArt",
                "ssl": True,
            },
        )
        assert tv.status_code == 201

        group = client.post(
            "/automation/groups",
            json={"name": "Downstairs", "tv_profile_ids": ["living_room"]},
        )
        assert group.status_code == 201
        group_id = group.json()["id"]
        playlist = client.post(
            "/automation/playlists",
            json={"name": "Landscapes", "job_ids": ["scheduled-one"]},
        )
        assert playlist.status_code == 201
        playlist_id = playlist.json()["id"]
        schedule = client.post(
            "/automation/schedules",
            json={
                "name": "Evening",
                "playlist_id": playlist_id,
                "group_id": group_id,
                "interval_seconds": 300,
            },
        )
        assert schedule.status_code == 201
        schedule_id = schedule.json()["id"]
        assert client.get("/automation/groups").json()[0]["name"] == "Downstairs"
        assert client.get("/automation/playlists").json()[0]["job_ids"] == [
            "scheduled-one"
        ]

        paused = client.put(
            f"/automation/schedules/{schedule_id}/enabled",
            json={"enabled": False},
        )
        assert paused.status_code == 200
        assert paused.json()["enabled"] is False
        assert client.delete(f"/automation/schedules/{schedule_id}").status_code == 200
        assert client.delete(f"/automation/playlists/{playlist_id}").status_code == 200
        assert client.delete(f"/automation/groups/{group_id}").status_code == 200

    @patch("frameart.api.display_artifact")
    def test_immediate_group_fanout(self, mock_display, managed_config_env):
        self.create_artifact(managed_config_env, "fanout-one")
        client.post(
            "/settings/tvs",
            json={
                "profile_id": "living_room",
                "ip": "192.168.1.51",
                "port": 8002,
                "client_name": "FrameArt",
                "ssl": True,
            },
        )
        group = client.post(
            "/automation/groups",
            json={"name": "One TV", "tv_profile_ids": ["living_room"]},
        ).json()
        mock_display.return_value = {
            "tv_profile_id": "living_room",
            "content_id": "content-one",
        }

        response = client.post(
            f"/automation/groups/{group['id']}/display",
            json={"job_id": "fanout-one", "matte": "none"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        mock_display.assert_called_once()

    def test_webhook_secret_is_only_returned_on_create(self, managed_config_env):
        created = client.post(
            "/automation/webhooks",
            json={
                "name": "HA",
                "url": "http://homeassistant.local/api/webhook/frameart",
                "events": ["schedule.completed"],
            },
        )
        assert created.status_code == 201
        assert len(created.json()["secret"]) == 64
        listed = client.get("/automation/webhooks")
        assert listed.status_code == 200
        assert "secret" not in listed.json()[0]

    def test_rejects_unknown_group_tv_and_missing_playlist_art(self, managed_config_env):
        unknown_tv = client.post(
            "/automation/groups",
            json={"name": "Bad", "tv_profile_ids": ["missing"]},
        )
        assert unknown_tv.status_code == 422
        missing_art = client.post(
            "/automation/playlists",
            json={"name": "Bad", "job_ids": ["missing-job"]},
        )
        assert missing_art.status_code == 422


# ---------------------------------------------------------------------------
# GET / — Web UI
# ---------------------------------------------------------------------------

class TestWebUI:
    def test_returns_html(self):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "FrameArt" in resp.text
        assert 'href="/static/app.css"' in resp.text
        assert 'src="/static/app.js"' in resp.text

        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200

    def test_tv_discovery_has_manual_fallback_and_http_error_handling(self):
        resp = client.get("/")
        script = client.get("/static/app.js")

        assert 'id="btn-add-tv"' in resp.text
        assert 'id="add-tv-modal"' in resp.text
        assert "parseJSONResponse(resp, 'TV discovery request failed.')" in script.text
        assert "frameart-api-lan" in script.text

    def test_settings_ui_has_provider_and_persistent_tv_management(self):
        resp = client.get("/")
        script = client.get("/static/app.js")

        assert 'id="btn-settings-add-provider"' in resp.text
        assert 'id="provider-settings-modal"' in resp.text
        assert 'id="btn-settings-add-tv"' in resp.text
        assert 'id="tv-settings-modal"' in resp.text
        assert "'/settings/providers'" in script.text
        assert "'/settings/tvs'" in script.text

    def test_automation_ui_has_groups_playlists_schedules_and_integrations(self):
        page = client.get("/")
        script = client.get("/static/app.js")

        assert 'data-page="automations"' in page.text
        assert 'id="automation-group-list"' in page.text
        assert 'id="automation-playlist-list"' in page.text
        assert 'id="automation-schedule-list"' in page.text
        assert 'id="automation-webhook-list"' in page.text
        assert "'/automation/groups'" in script.text
        assert "'/automation/schedules'" in script.text
