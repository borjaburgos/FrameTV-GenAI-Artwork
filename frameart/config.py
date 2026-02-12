"""Configuration management for FrameArt.

Config sources (in priority order):
1. CLI flags (passed directly to functions)
2. Environment variables (FRAMEART_ prefix)
3. config.yaml file
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    port: int = 8002
    name: str = "FrameArt"
    token_file: str | None = None
    ssl: bool = True


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


def load_settings(**overrides: Any) -> Settings:
    """Load settings from config file + env vars + explicit overrides.

    Overrides are applied last and take highest priority.
    """
    file_data: dict[str, Any] = {}
    config_path = _find_config_file()
    if config_path:
        file_data = _load_yaml_config(config_path)

    # Merge: file data is the base, env vars and overrides layer on top.
    # Pydantic-settings reads env vars automatically; we inject file data as init kwargs.
    merged = {**file_data, **{k: v for k, v in overrides.items() if v is not None}}
    return Settings(**merged)
