"""Ollama-based image generation adapter.

Ollama primarily serves LLMs. For text-to-image, this adapter assumes either:
- An Ollama-compatible image generation endpoint on the LAN, or
- A ComfyUI / Stable Diffusion WebUI API running alongside Ollama.

The adapter calls a configurable HTTP endpoint that accepts a JSON payload
with a ``prompt`` field and returns an image.
"""

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


class OllamaProvider(ImageProvider):
    """Generate images via a local Ollama-compatible image endpoint."""

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._config = config or ProviderConfig()
        self._base_url = (
            self._config.base_url
            or os.environ.get("OLLAMA_BASE_URL")
            or "http://localhost:11434"
        )
        self._model = self._config.model or "sdxl"
        self._timeout = self._config.timeout

    @property
    def name(self) -> str:
        return "ollama"

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
        payload: dict[str, Any] = {
            "prompt": prompt,
            "model": self._model,
        }
        if width:
            payload["width"] = width
        if height:
            payload["height"] = height
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if seed is not None:
            payload["seed"] = seed
        if steps is not None:
            payload["steps"] = steps
        if guidance is not None:
            payload["guidance_scale"] = guidance

        # Try the /api/generate endpoint (common for image-generation wrappers)
        url = f"{self._base_url}/api/generate"
        logger.info("Ollama generate: url=%s model=%s", url, self._model)

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()

        data = resp.json()

        # Support multiple response formats:
        # 1. { "images": ["base64..."] }
        # 2. { "image": "base64..." }
        # 3. raw image bytes (content-type: image/*)
        if "images" in data:
            image_bytes = base64.b64decode(data["images"][0])
        elif "image" in data:
            image_bytes = base64.b64decode(data["image"])
        else:
            # Assume the response body is the raw image
            image_bytes = resp.content

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        logger.info("Ollama returned image %dx%d", w, h)

        return GeneratedImage(
            data=image_bytes,
            mime_type=f"image/{img.format.lower()}" if img.format else "image/png",
            width=w,
            height=h,
            metadata={
                "provider": self.name,
                "model": self._model,
                "base_url": self._base_url,
            },
        )
