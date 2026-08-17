"""Tests for persistent web-managed settings storage."""

from __future__ import annotations

import stat

from frameart.settings_store import (
    load_managed_overlay,
    managed_settings_path,
    provider_secrets_path,
    read_managed_settings,
    read_provider_secrets,
    update_management_state,
)


def test_update_management_state_writes_overlay_and_restricted_secrets(tmp_path):
    def update(settings, provider_keys):
        settings["default_provider"] = "openai"
        settings["providers"] = {"openai": {"model": "gpt-image-1"}}
        provider_keys["openai"] = "secret-key"

    update_management_state(tmp_path, update)

    assert read_managed_settings(tmp_path)["default_provider"] == "openai"
    assert read_provider_secrets(tmp_path) == {"openai": "secret-key"}
    assert stat.S_IMODE(managed_settings_path(tmp_path).stat().st_mode) == 0o600
    assert stat.S_IMODE(provider_secrets_path(tmp_path).stat().st_mode) == 0o600

    overlay = load_managed_overlay(tmp_path)
    assert overlay["providers"]["openai"]["model"] == "gpt-image-1"
    assert overlay["providers"]["openai"]["api_key"] == "secret-key"


def test_update_management_state_preserves_existing_sections(tmp_path):
    update_management_state(
        tmp_path,
        lambda settings, _keys: settings.update({"tvs": {"living_room": {"ip": "10.0.0.5"}}}),
    )
    update_management_state(
        tmp_path,
        lambda settings, _keys: settings.update({"default_provider": "ollama"}),
    )

    managed = read_managed_settings(tmp_path)
    assert managed["default_provider"] == "ollama"
    assert managed["tvs"]["living_room"]["ip"] == "10.0.0.5"
