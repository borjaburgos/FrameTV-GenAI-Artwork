"""Tests for provider interface contracts and registry."""

from __future__ import annotations

import pytest

from frameart.providers.base import GeneratedImage, ImageProvider
from frameart.providers.registry import available_providers, get_provider


class TestProviderRegistry:
    def test_available_providers(self):
        providers = available_providers()
        assert "openai" in providers
        assert "ollama" in providers
        assert "gemini" in providers
        assert "anthropic" in providers

    def test_get_provider_openai(self):
        provider = get_provider("openai")
        assert isinstance(provider, ImageProvider)
        assert provider.name == "openai"

    def test_get_provider_ollama(self):
        provider = get_provider("ollama")
        assert isinstance(provider, ImageProvider)
        assert provider.name == "ollama"

    def test_get_unknown_provider(self):
        with pytest.raises(KeyError, match="Unknown provider"):
            get_provider("nonexistent")


class TestStubProviders:
    def test_gemini_not_implemented(self):
        provider = get_provider("gemini")
        with pytest.raises(NotImplementedError):
            provider.generate("test prompt")

    def test_anthropic_not_implemented(self):
        provider = get_provider("anthropic")
        with pytest.raises(NotImplementedError):
            provider.generate("test prompt")


class TestGeneratedImage:
    def test_dataclass(self):
        img = GeneratedImage(
            data=b"\x89PNG",
            mime_type="image/png",
            width=100,
            height=100,
        )
        assert img.data == b"\x89PNG"
        assert img.mime_type == "image/png"
        assert img.width == 100
        assert img.height == 100
        assert img.metadata == {}

    def test_with_metadata(self):
        img = GeneratedImage(
            data=b"",
            mime_type="image/jpeg",
            width=1024,
            height=1024,
            metadata={"model": "dall-e-3"},
        )
        assert img.metadata["model"] == "dall-e-3"
