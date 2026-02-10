"""Google Gemini / Imagen image generation adapter (stub).

TODO: Implement when Imagen API access is available.
"""

from __future__ import annotations

import logging
from typing import Any

from frameart.config import ProviderConfig
from frameart.providers.base import GeneratedImage, ImageProvider

logger = logging.getLogger(__name__)


class GeminiProvider(ImageProvider):
    """Generate images via Google's Gemini / Imagen API.

    This is a stub — implement when the API is available.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._config = config or ProviderConfig()

    @property
    def name(self) -> str:
        return "gemini"

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
            "Gemini/Imagen adapter is not yet implemented. "
            "Contributions welcome — see providers/gemini_adapter.py."
        )
