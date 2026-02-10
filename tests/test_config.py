"""Tests for configuration loading."""

from __future__ import annotations

import os

import yaml

from frameart.config import STYLE_PRESETS, Settings, TVProfile, load_settings


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
