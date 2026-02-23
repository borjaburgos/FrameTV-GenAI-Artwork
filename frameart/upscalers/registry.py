"""Upscaler registry — maps upscaler names to adapter classes."""

from __future__ import annotations

from frameart.config import UpscalerConfig
from frameart.upscalers.base import Upscaler

_REGISTRY: dict[str, type[Upscaler]] = {}


def _populate_registry() -> None:
    from frameart.upscalers.local_http import LocalHTTPUpscaler
    from frameart.upscalers.none_upscaler import NoneUpscaler
    from frameart.upscalers.remote_http import RemoteHTTPUpscaler

    _REGISTRY["none"] = NoneUpscaler
    _REGISTRY["local_http"] = LocalHTTPUpscaler
    _REGISTRY["remote_http"] = RemoteHTTPUpscaler


def get_upscaler(name: str, config: UpscalerConfig | None = None) -> Upscaler:
    """Instantiate and return an upscaler by name."""
    if not _REGISTRY:
        _populate_registry()

    cls = _REGISTRY.get(name)
    if cls is None:
        available = ", ".join(sorted(_REGISTRY.keys()))
        raise KeyError(f"Unknown upscaler '{name}'. Available: {available}")

    if name == "none":
        return cls()
    return cls(config)


def available_upscalers() -> list[str]:
    """Return sorted list of registered upscaler names."""
    if not _REGISTRY:
        _populate_registry()
    return sorted(_REGISTRY.keys())
