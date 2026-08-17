"""Persistent web-managed settings and provider secrets.

The web UI writes a small overlay beneath ``data_dir`` instead of modifying a
possibly read-only user ``config.yaml``. Environment variables remain the
highest-priority settings source.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

_STORE_LOCK = threading.RLock()
_MANAGED_SETTINGS_RELATIVE_PATH = Path("settings") / "managed.yaml"
_PROVIDER_SECRETS_RELATIVE_PATH = Path("secrets") / "provider-keys.yaml"


def managed_settings_path(data_dir: Path) -> Path:
    """Return the non-secret managed settings overlay path."""
    return Path(data_dir) / _MANAGED_SETTINGS_RELATIVE_PATH


def provider_secrets_path(data_dir: Path) -> Path:
    """Return the provider API-key store path."""
    return Path(data_dir) / _PROVIDER_SECRETS_RELATIVE_PATH


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as stream:
        payload = yaml.safe_load(stream)
    return payload if isinstance(payload, dict) else {}


def read_managed_settings(data_dir: Path) -> dict[str, Any]:
    """Read the web-managed non-secret overlay."""
    with _STORE_LOCK:
        return _read_yaml_mapping(managed_settings_path(data_dir))


def read_provider_secrets(data_dir: Path) -> dict[str, str]:
    """Read managed provider keys as a provider-name mapping."""
    with _STORE_LOCK:
        payload = _read_yaml_mapping(provider_secrets_path(data_dir))
        providers = payload.get("providers")
        if not isinstance(providers, dict):
            return {}
        return {
            str(name): value
            for name, value in providers.items()
            if isinstance(name, str) and isinstance(value, str) and value
        }


def load_managed_overlay(data_dir: Path) -> dict[str, Any]:
    """Load managed settings with provider secrets injected for validation."""
    with _STORE_LOCK:
        overlay = _read_yaml_mapping(managed_settings_path(data_dir))
        secrets = read_provider_secrets(data_dir)
        if not secrets:
            return overlay

        providers = overlay.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            overlay["providers"] = providers
        for name, api_key in secrets.items():
            provider = providers.get(name)
            if not isinstance(provider, dict):
                provider = {}
                providers[name] = provider
            provider["api_key"] = api_key
        return overlay


def _atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temp_path = Path(stream.name)
        try:
            os.chmod(temp_path, 0o600)
            yaml.safe_dump(payload, stream, sort_keys=False, default_flow_style=False)
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    try:
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def update_management_state(
    data_dir: Path,
    updater: Callable[[dict[str, Any], dict[str, str]], None],
) -> None:
    """Atomically replace each managed file after applying ``updater`` under a lock."""
    with _STORE_LOCK:
        settings = _read_yaml_mapping(managed_settings_path(data_dir))
        secrets = read_provider_secrets(data_dir)
        updater(settings, secrets)
        _atomic_write_yaml(managed_settings_path(data_dir), settings)
        _atomic_write_yaml(provider_secrets_path(data_dir), {"providers": secrets})
