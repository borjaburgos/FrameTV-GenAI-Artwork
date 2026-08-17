"""Configuration management for FrameArt.

Config sources (in priority order):
1. CLI flags (passed directly to functions)
2. Environment variables (FRAMEART_ prefix)
3. config.yaml file
"""

from __future__ import annotations

import os
from ipaddress import IPv4Address, IPv4Network, ip_address
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from frameart.settings_store import load_managed_overlay, read_managed_settings


def _default_data_dir() -> Path:
    """Pick a sensible default data directory.

    - If FRAMEART_DATA_DIR is set, use it.
    - If /data/frameart exists (Docker/LXC), use it.
    - Otherwise fall back to ~/.local/share/frameart (works on macOS/Linux).
    """
    env = os.environ.get("FRAMEART_DATA_DIR")
    if env:
        return Path(env)
    container_path = Path("/data/frameart")
    if container_path.exists():
        return container_path
    return Path("~/.local/share/frameart").expanduser()


DEFAULT_DATA_DIR = _default_data_dir()
DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path("~/.config/frameart/config.yaml").expanduser(),
    Path("/etc/frameart/config.yaml"),
]

# --- Style presets -----------------------------------------------------------

STYLE_PRESETS: dict[str, str] = {
    "abstract": "in an abstract art style with bold colors and geometric shapes",
    "kid_drawing": "as drawn by an 8 year-old child with crayons, naive art style",
    "watercolor": "as a watercolor painting with soft edges and translucent washes",
    "bw_photo": "as a black-and-white photograph, high contrast, dramatic lighting",
    "oil_painting": "as a classical oil painting with rich textures and depth",
    "pixel_art": "in pixel art style with a retro video game aesthetic",
    "impressionist": "in the style of French impressionism, loose brushstrokes, natural light",
    "minimalist": "in a minimalist style with clean lines and limited color palette",
}


# --- Pydantic config models --------------------------------------------------


class TVProfile(BaseModel):
    """Configuration for a single Samsung Frame TV."""

    ip: str
    port: int = Field(8002, ge=1, le=65535)
    name: str = "FrameArt"
    token_file: str | None = None
    ssl: bool = True

    @field_validator("ip")
    @classmethod
    def validate_private_ipv4(cls, value: str) -> str:
        """Samsung TV control is intentionally limited to private IPv4 networks."""
        try:
            address = ip_address(value.strip())
        except ValueError as exc:
            raise ValueError("TV IP must be a valid IPv4 address") from exc

        private_networks = (
            IPv4Network("10.0.0.0/8"),
            IPv4Network("172.16.0.0/12"),
            IPv4Network("192.168.0.0/16"),
        )
        if not isinstance(address, IPv4Address) or not any(
            address in network for network in private_networks
        ):
            raise ValueError("TV IP must be an RFC1918 private IPv4 address")
        return str(address)


class ProviderConfig(BaseModel):
    """Configuration for an image generation provider."""

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    timeout: int = 120
    extra: dict[str, Any] = Field(default_factory=dict)


class UpscalerConfig(BaseModel):
    """Configuration for an upscaler."""

    base_url: str | None = None
    api_key: str | None = None
    timeout: int = 120


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_prefix="FRAMEART_",
        env_nested_delimiter="__",
    )

    # Version of the web-managed settings overlay. This is persisted so future
    # releases can migrate management state explicitly.
    schema_version: int = Field(1, ge=1)

    # Directories
    data_dir: Path = DEFAULT_DATA_DIR

    # Default provider
    default_provider: str = "openai"
    default_model: str | None = None

    # Default upscaler
    default_upscaler: str = "none"

    # TVs — keyed by profile name
    tvs: dict[str, TVProfile] = Field(default_factory=dict)

    # Provider configs — keyed by provider name
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)

    # Upscaler configs — keyed by upscaler name
    upscalers: dict[str, UpscalerConfig] = Field(default_factory=dict)

    # Logging
    log_level: str = "INFO"
    log_file: str | None = None

    # Pipeline defaults
    default_style: str | None = None
    auto_aspect_hint: bool = True

    # HTTP API security. Loopback-only serving may leave auth disabled; the
    # server refuses non-loopback binds unless auth is enabled.
    auth_enabled: bool = False
    admin_token: SecretStr | None = None
    automation_token: SecretStr | None = None
    api_rate_limit_per_minute: int = Field(60, ge=1, le=10_000)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Make the documented precedence explicit: args > env > YAML."""
        del dotenv_settings
        return (
            init_settings,
            env_settings,
            _ManagedSettingsSource(settings_cls),
            _YamlSettingsSource(settings_cls),
            file_secret_settings,
        )


def _find_config_file() -> Path | None:
    """Return the first existing config file, or None."""
    env_path = os.environ.get("FRAMEART_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    return None


def _load_yaml_config(path: Path) -> dict[str, Any]:
    """Load a YAML config file and return its contents as a dict."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Read the first configured YAML file as the lowest-priority source."""

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        data = self()
        return data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        config_path = _find_config_file()
        if not config_path:
            return {}
        config = _load_yaml_config(config_path)
        managed = read_managed_settings(_managed_data_dir())
        # Provider/TV management snapshots are complete collections. Suppress
        # their lower-priority YAML versions so deletions are not deep-merged
        # back into the effective settings by pydantic-settings.
        for collection in ("providers", "tvs"):
            if collection in managed:
                config.pop(collection, None)
        return config


def _managed_data_dir() -> Path:
    """Resolve the data directory without recursively constructing ``Settings``."""
    env_data_dir = os.environ.get("FRAMEART_DATA_DIR")
    if env_data_dir:
        return Path(env_data_dir).expanduser()

    config_path = _find_config_file()
    if config_path:
        configured = _load_yaml_config(config_path).get("data_dir")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser()
    return DEFAULT_DATA_DIR


class _ManagedSettingsSource(PydanticBaseSettingsSource):
    """Load the web-managed overlay above YAML and below environment values."""

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        data = self()
        return data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return load_managed_overlay(_managed_data_dir())


def load_settings(**overrides: Any) -> Settings:
    """Load settings from config file + env vars + explicit overrides.

    Overrides are applied last and take highest priority.
    """
    return Settings(**{k: v for k, v in overrides.items() if v is not None})
