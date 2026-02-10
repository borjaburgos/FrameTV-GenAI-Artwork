"""OpenAI DALL-E image generation adapter."""

from __future__ import annotations

import base64
import io
import logging
import os
from typing import Any

import httpx
from PIL import Image

from frameart.config import ProviderConfig
from frameart.providers.base import GeneratedImage, ImageProvider

logger = logging.getLogger(__name__)

# DALL-E 3 supported sizes
_DALLE3_SIZES = {
    "1024x1024": (1024, 1024),
    "1792x1024": (1792, 1024),  # landscape — closest to 16:9
    "1024x1792": (1024, 1792),
}


class OpenAIProvider(ImageProvider):
    """Generate images via OpenAI's DALL-E API."""

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._config = config or ProviderConfig()
        self._api_key = (
            self._config.api_key
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        self._base_url = self._config.base_url or "https://api.openai.com/v1"
        self._model = self._config.model or "dall-e-3"
        self._timeout = self._config.timeout

    @property
    def name(self) -> str:
        return "openai"

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
        if not self._api_key:
            raise RuntimeError(
                "OpenAI API key not set. "
                "Set OPENAI_API_KEY env var or configure providers.openai.api_key"
            )

        # Pick the best DALL-E 3 size — landscape for 16:9 content
        size = "1792x1024"
        if width and height and height > width:
            size = "1024x1792"

        quality = kwargs.get("quality", "hd")
        style = kwargs.get("dalle_style", "vivid")

        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
            "style": style,
            "response_format": "b64_json",
        }

        logger.info("OpenAI generate: model=%s size=%s quality=%s", self._model, size, quality)

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self._base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()

        data = resp.json()
        b64_data = data["data"][0]["b64_json"]
        revised_prompt = data["data"][0].get("revised_prompt", prompt)
        image_bytes = base64.b64decode(b64_data)

        # Detect actual dimensions
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        logger.info("OpenAI returned image %dx%d", w, h)

        return GeneratedImage(
            data=image_bytes,
            mime_type="image/png",
            width=w,
            height=h,
            metadata={
                "provider": self.name,
                "model": self._model,
                "size_requested": size,
                "quality": quality,
                "style": style,
                "revised_prompt": revised_prompt,
            },
        )
