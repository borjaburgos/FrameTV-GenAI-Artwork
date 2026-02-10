"""Anthropic image generation adapter (stub).

TODO: Implement when Anthropic offers a direct image generation API.
"""

from __future__ import annotations

import logging
from typing import Any

from frameart.config import ProviderConfig
from frameart.providers.base import GeneratedImage, ImageProvider

logger = logging.getLogger(__name__)


class AnthropicProvider(ImageProvider):
    """Generate images via Anthropic's API.

    This is a stub — implement when an image generation API is available.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._config = config or ProviderConfig()

    @property
    def name(self) -> str:
        return "anthropic"

    def generate(
        self,
        prompt: str,
        *,
        width: int | None = None,
        height: int | None = None,
        negative_prompt: str | None = None,
        seed: int | None = None,
        steps: int | None = None,
        guidance: float | None = None,
        **kwargs: Any,
    ) -> GeneratedImage:
        raise NotImplementedError(
            "Anthropic image generation adapter is not yet implemented. "
            "Contributions welcome — see providers/anthropic_adapter.py."
        )
