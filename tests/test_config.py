"""Tests for configuration loading."""

from __future__ import annotations

import os

import pytest
import yaml
from pydantic import ValidationError

from frameart.config import STYLE_PRESETS, Settings, TVProfile, load_settings
from frameart.settings_store import update_management_state


class TestSettings:
    def test_defaults(self):
        settings = Settings()
        assert settings.default_provider == "openai"
        assert settings.default_upscaler == "none"
        assert settings.auto_aspect_hint is True
        assert settings.log_level == "INFO"

    def test_tv_profile_defaults(self):
        profile = TVProfile(ip="192.168.1.100")
        assert profile.port == 8002
        assert profile.name == "FrameArt"
        assert profile.ssl is True

    @pytest.mark.parametrize("ip", ["8.8.8.8", "127.0.0.1", "not-an-ip", "::1"])
    def test_tv_profile_rejects_non_private_ipv4(self, ip):
        with pytest.raises(ValidationError):
            TVProfile(ip=ip)


class TestStylePresets:
    def test_presets_not_empty(self):
        assert len(STYLE_PRESETS) > 0

    def test_known_presets(self):
        assert "abstract" in STYLE_PRESETS
        assert "kid_drawing" in STYLE_PRESETS
        assert "oil_painting" in STYLE_PRESETS

    def test_presets_are_strings(self):
        for _name, text in STYLE_PRESETS.items():
            assert isinstance(text, str)
            assert len(text) > 0


class TestLoadSettings:
    def test_load_without_config_file(self):
        settings = load_settings()
        assert settings.default_provider == "openai"

    def test_load_with_overrides(self):
        settings = load_settings(default_provider="ollama", log_level="DEBUG")
        assert settings.default_provider == "ollama"
        assert settings.log_level == "DEBUG"

    def test_load_from_yaml(self, tmp_path):
        config_data = {
            "default_provider": "ollama",
            "tvs": {
                "test_tv": {
                    "ip": "10.0.0.1",
                    "port": 8002,
                }
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        os.environ["FRAMEART_CONFIG"] = str(config_file)
        try:
            settings = load_settings()
            assert settings.default_provider == "ollama"
            assert "test_tv" in settings.tvs
            assert settings.tvs["test_tv"].ip == "10.0.0.1"
        finally:
            del os.environ["FRAMEART_CONFIG"]

    def test_environment_overrides_yaml(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("default_provider: ollama\nlog_level: WARNING\n")
        monkeypatch.setenv("FRAMEART_CONFIG", str(config_file))
        monkeypatch.setenv("FRAMEART_DEFAULT_PROVIDER", "openai")

        settings = load_settings()

        assert settings.default_provider == "openai"
        assert settings.log_level == "WARNING"

    def test_managed_settings_override_yaml_but_not_environment(self, tmp_path, monkeypatch):
        config_file = tmp_path / "base.yaml"
        config_file.write_text(
            "default_provider: openai\n"
            "providers:\n"
            "  openai:\n"
            "    model: dall-e-3\n"
        )
        monkeypatch.setenv("FRAMEART_CONFIG", str(config_file))
        monkeypatch.setenv("FRAMEART_DATA_DIR", str(tmp_path))

        def update(settings, provider_keys):
            settings["default_provider"] = "google"
            settings["providers"] = {"google": {"model": "nano-banana"}}
            provider_keys["google"] = "managed-secret"

        update_management_state(tmp_path, update)

        settings = load_settings()
        assert settings.default_provider == "google"
        assert set(settings.providers) == {"google"}
        assert settings.providers["google"].api_key == "managed-secret"

        monkeypatch.setenv("FRAMEART_DEFAULT_PROVIDER", "ollama")
        assert load_settings().default_provider == "ollama"
