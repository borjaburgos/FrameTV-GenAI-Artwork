"""Persistent web-managed settings and provider secrets.

The web UI writes a small overlay beneath ``data_dir`` instead of modifying a
possibly read-only user ``config.yaml``. Environment variables remain the
highest-priority settings source.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_STORE_LOCK = threading.RLock()
SETTINGS_SCHEMA_VERSION = 1
_MAX_SETTINGS_BACKUPS = 20
_MANAGED_SETTINGS_RELATIVE_PATH = Path("settings") / "managed.yaml"
_PROVIDER_SECRETS_RELATIVE_PATH = Path("secrets") / "provider-keys.yaml"
_INTEGRATION_SECRETS_RELATIVE_PATH = Path("secrets") / "integration-keys.yaml"
_TRANSACTION_RELATIVE_PATH = Path("settings") / ".management-transaction.yaml"
_BACKUPS_RELATIVE_PATH = Path("backups") / "settings"
_BACKUP_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")


def managed_settings_path(data_dir: Path) -> Path:
    """Return the non-secret managed settings overlay path."""
    return Path(data_dir) / _MANAGED_SETTINGS_RELATIVE_PATH


def provider_secrets_path(data_dir: Path) -> Path:
    """Return the provider API-key store path."""
    return Path(data_dir) / _PROVIDER_SECRETS_RELATIVE_PATH


def integration_secrets_path(data_dir: Path) -> Path:
    """Return the non-generation integration-key store path."""
    return Path(data_dir) / _INTEGRATION_SECRETS_RELATIVE_PATH


def management_transaction_path(data_dir: Path) -> Path:
    """Return the crash-recovery journal path for managed settings writes."""
    return Path(data_dir) / _TRANSACTION_RELATIVE_PATH


def settings_backups_path(data_dir: Path) -> Path:
    """Return the directory containing bounded settings snapshots."""
    return Path(data_dir) / _BACKUPS_RELATIVE_PATH


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as stream:
        payload = yaml.safe_load(stream)
    return payload if isinstance(payload, dict) else {}


def read_managed_settings(data_dir: Path) -> dict[str, Any]:
    """Read the web-managed non-secret overlay."""
    with _STORE_LOCK:
        recover_management_state(data_dir)
        return _read_yaml_mapping(managed_settings_path(data_dir))


def read_provider_secrets(data_dir: Path) -> dict[str, str]:
    """Read managed provider keys as a provider-name mapping."""
    with _STORE_LOCK:
        recover_management_state(data_dir)
        payload = _read_yaml_mapping(provider_secrets_path(data_dir))
        providers = payload.get("providers")
        if not isinstance(providers, dict):
            return {}
        return {
            str(name): value
            for name, value in providers.items()
            if isinstance(name, str) and isinstance(value, str) and value
        }


def read_integration_secrets(data_dir: Path) -> dict[str, str]:
    """Read managed keys for integrations such as TheSportsDB."""
    with _STORE_LOCK:
        recover_management_state(data_dir)
        payload = _read_yaml_mapping(integration_secrets_path(data_dir))
        integrations = payload.get("integrations")
        if not isinstance(integrations, dict):
            return {}
        return {
            str(name): value
            for name, value in integrations.items()
            if isinstance(name, str) and isinstance(value, str) and value
        }


def load_managed_overlay(data_dir: Path) -> dict[str, Any]:
    """Load managed settings with provider secrets injected for validation."""
    with _STORE_LOCK:
        recover_management_state(data_dir)
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


def _provider_mapping(payload: dict[str, Any]) -> dict[str, str]:
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        return {}
    return {
        str(name): value
        for name, value in providers.items()
        if isinstance(name, str) and isinstance(value, str) and value
    }


def _integration_mapping(payload: dict[str, Any]) -> dict[str, str]:
    integrations = payload.get("integrations")
    if not isinstance(integrations, dict):
        return {}
    return {
        str(name): value
        for name, value in integrations.items()
        if isinstance(name, str) and isinstance(value, str) and value
    }


def _journal_payload(
    settings: dict[str, Any],
    secrets: dict[str, str],
    integration_secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    normalized_settings = dict(settings)
    normalized_settings["schema_version"] = SETTINGS_SCHEMA_VERSION
    payload = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "settings": normalized_settings,
        "provider_keys": {"providers": dict(secrets)},
    }
    if integration_secrets is not None:
        payload["integration_keys"] = {"integrations": dict(integration_secrets)}
    return payload


def recover_management_state(data_dir: Path) -> bool:
    """Complete an interrupted two-file settings transaction, if one exists.

    The journal is written before either destination file. Replaying it is
    idempotent, so a process crash can leave the old state or the new state on
    disk temporarily, but the next read always converges on the complete new
    state before it is returned.
    """
    with _STORE_LOCK:
        journal_path = management_transaction_path(data_dir)
        if not journal_path.is_file():
            return False
        journal = _read_yaml_mapping(journal_path)
        settings = journal.get("settings")
        provider_keys = journal.get("provider_keys")
        integration_keys = journal.get("integration_keys")
        if (
            journal.get("schema_version") != SETTINGS_SCHEMA_VERSION
            or not isinstance(settings, dict)
            or not isinstance(provider_keys, dict)
            or (integration_keys is not None and not isinstance(integration_keys, dict))
        ):
            raise ValueError("Managed settings recovery journal is invalid or unsupported.")
        _atomic_write_yaml(managed_settings_path(data_dir), settings)
        _atomic_write_yaml(provider_secrets_path(data_dir), provider_keys)
        if integration_keys is not None:
            _atomic_write_yaml(integration_secrets_path(data_dir), integration_keys)
        journal_path.unlink()
        return True


def _backup_path(data_dir: Path, backup_id: str) -> Path:
    if not _BACKUP_ID_RE.fullmatch(backup_id):
        raise ValueError("Invalid settings backup ID.")
    return settings_backups_path(data_dir) / f"{backup_id}.yaml"


def _prune_settings_backups(data_dir: Path) -> None:
    paths = sorted(settings_backups_path(data_dir).glob("*.yaml"), reverse=True)
    for stale_path in paths[_MAX_SETTINGS_BACKUPS:]:
        stale_path.unlink(missing_ok=True)


def create_settings_backup(data_dir: Path, *, reason: str = "manual") -> dict[str, str]:
    """Create a restricted snapshot of settings and all managed API keys."""
    with _STORE_LOCK:
        recover_management_state(data_dir)
        created_at = datetime.now(timezone.utc).replace(microsecond=0)
        backup_id = f"{created_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        settings = _read_yaml_mapping(managed_settings_path(data_dir))
        secrets_payload = _read_yaml_mapping(provider_secrets_path(data_dir))
        integration_payload = _read_yaml_mapping(integration_secrets_path(data_dir))
        payload = {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "backup_id": backup_id,
            "created_at": created_at.isoformat(),
            "reason": reason[:100],
            "state": _journal_payload(
                settings,
                _provider_mapping(secrets_payload),
                _integration_mapping(integration_payload),
            ),
        }
        _atomic_write_yaml(_backup_path(data_dir, backup_id), payload)
        _prune_settings_backups(data_dir)
        return {
            "backup_id": backup_id,
            "created_at": payload["created_at"],
            "reason": payload["reason"],
        }


def list_settings_backups(data_dir: Path) -> list[dict[str, str]]:
    """List settings snapshots without exposing their secret contents."""
    with _STORE_LOCK:
        backups: list[dict[str, str]] = []
        for path in sorted(settings_backups_path(data_dir).glob("*.yaml"), reverse=True):
            payload = _read_yaml_mapping(path)
            backup_id = payload.get("backup_id")
            if not isinstance(backup_id, str) or not _BACKUP_ID_RE.fullmatch(backup_id):
                continue
            backups.append(
                {
                    "backup_id": backup_id,
                    "created_at": str(payload.get("created_at") or ""),
                    "reason": str(payload.get("reason") or "unknown"),
                }
            )
        return backups


def replace_management_state(
    data_dir: Path,
    settings: dict[str, Any],
    provider_keys: dict[str, str],
    *,
    integration_keys: dict[str, str] | None = None,
    backup_reason: str | None = "before-update",
) -> None:
    """Replace managed settings and secrets using a replayable journal."""
    with _STORE_LOCK:
        recover_management_state(data_dir)
        if integration_keys is None:
            integration_keys = read_integration_secrets(data_dir)
        if backup_reason and (
            managed_settings_path(data_dir).is_file()
            or provider_secrets_path(data_dir).is_file()
            or integration_secrets_path(data_dir).is_file()
        ):
            create_settings_backup(data_dir, reason=backup_reason)
        transaction = _journal_payload(settings, provider_keys, integration_keys)
        journal_path = management_transaction_path(data_dir)
        _atomic_write_yaml(journal_path, transaction)
        _atomic_write_yaml(managed_settings_path(data_dir), transaction["settings"])
        _atomic_write_yaml(provider_secrets_path(data_dir), transaction["provider_keys"])
        _atomic_write_yaml(integration_secrets_path(data_dir), transaction["integration_keys"])
        journal_path.unlink()


def restore_settings_backup(data_dir: Path, backup_id: str) -> dict[str, str]:
    """Restore a snapshot after first preserving the current state."""
    with _STORE_LOCK:
        payload = _read_yaml_mapping(_backup_path(data_dir, backup_id))
        if payload.get("schema_version") != SETTINGS_SCHEMA_VERSION:
            raise ValueError("Settings backup schema is invalid or unsupported.")
        state = payload.get("state")
        if not isinstance(state, dict):
            raise ValueError("Settings backup is invalid.")
        settings = state.get("settings")
        provider_keys = state.get("provider_keys")
        integration_keys = state.get("integration_keys")
        if not isinstance(settings, dict) or not isinstance(provider_keys, dict):
            raise ValueError("Settings backup is invalid.")
        if integration_keys is not None and not isinstance(integration_keys, dict):
            raise ValueError("Settings backup is invalid.")
        replace_management_state(
            data_dir,
            settings,
            _provider_mapping(provider_keys),
            integration_keys=(
                _integration_mapping(integration_keys)
                if integration_keys is not None
                else read_integration_secrets(data_dir)
            ),
            backup_reason="before-restore",
        )
        return {
            "backup_id": str(payload.get("backup_id") or backup_id),
            "created_at": str(payload.get("created_at") or ""),
            "reason": str(payload.get("reason") or "unknown"),
        }


def update_management_state(
    data_dir: Path,
    updater: Callable[[dict[str, Any], dict[str, str]], None],
) -> None:
    """Apply an update and persist the pair through the recovery journal."""
    with _STORE_LOCK:
        recover_management_state(data_dir)
        settings = _read_yaml_mapping(managed_settings_path(data_dir))
        secrets = read_provider_secrets(data_dir)
        updater(settings, secrets)
        replace_management_state(data_dir, settings, secrets)


def update_integration_secret(data_dir: Path, name: str, value: str | None) -> None:
    """Atomically set or clear one managed integration secret."""
    with _STORE_LOCK:
        recover_management_state(data_dir)
        settings = _read_yaml_mapping(managed_settings_path(data_dir))
        provider_keys = read_provider_secrets(data_dir)
        integration_keys = read_integration_secrets(data_dir)
        if value:
            integration_keys[name] = value
        else:
            integration_keys.pop(name, None)
        replace_management_state(
            data_dir,
            settings,
            provider_keys,
            integration_keys=integration_keys,
        )
