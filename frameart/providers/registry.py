"""Provider registry — maps provider names to adapter classes."""

from __future__ import annotations

from frameart.config import ProviderConfig
from frameart.providers.base import ImageProvider

_REGISTRY: dict[str, type[ImageProvider]] = {}


def _populate_registry() -> None:
    """Lazily import and register all built-in providers."""
    from frameart.providers.ollama_adapter import OllamaProvider
    from frameart.providers.openai_adapter import OpenAIProvider

    _REGISTRY["openai"] = OpenAIProvider
    _REGISTRY["ollama"] = OllamaProvider


def get_provider(name: str, config: ProviderConfig | None = None) -> ImageProvider:
    """Instantiate and return a provider by name.

    Raises ``KeyError`` if the provider name is unknown.
    """
    if not _REGISTRY:
        _populate_registry()

    cls = _REGISTRY.get(name)
    if cls is None:
        available = ", ".join(sorted(_REGISTRY.keys()))
        raise KeyError(f"Unknown provider '{name}'. Available: {available}")

    return cls(config)


def available_providers() -> list[str]:
    """Return sorted list of registered provider names."""
    if not _REGISTRY:
        _populate_registry()
    return sorted(_REGISTRY.keys())
