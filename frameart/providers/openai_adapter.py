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
_DALLE3_SIZES = {"1024x1024", "1792x1024", "1024x1792"}

# gpt-image-1 supported sizes
_GPT_IMAGE_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}


class OpenAIProvider(ImageProvider):
    """Generate images via OpenAI's image generation API."""

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

    def _build_payload(
        self,
        prompt: str,
        width: int | None,
        height: int | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the API payload based on the model."""
        model = self._model

        if model.startswith("gpt-image"):
            # gpt-image-1: landscape = 1536x1024
            size = "1536x1024"
            if width and height and height > width:
                size = "1024x1536"

            payload: dict[str, Any] = {
                "model": model,
                "prompt": prompt,
                "n": 1,
                "size": size,
            }
            quality = kwargs.get("quality", "high")
            if quality:
                payload["quality"] = quality

        else:
            # dall-e-3 / dall-e-2
            size = "1792x1024"
            if width and height and height > width:
                size = "1024x1792"

            quality = kwargs.get("quality", "hd")
            style = kwargs.get("dalle_style", "vivid")

            payload = {
                "model": model,
                "prompt": prompt,
                "n": 1,
                "size": size,
                "quality": quality,
                "style": style,
                "response_format": "b64_json",
            }

        return payload

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
                "Set OPENAI_API_KEY env var or providers.openai.api_key"
            )

        payload = self._build_payload(prompt, width, height, **kwargs)

        logger.info(
            "OpenAI generate: model=%s size=%s",
            payload["model"], payload.get("size"),
        )

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self._base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        # Parse error body for a useful message instead of generic 400
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text)
            except Exception:
                err_msg = resp.text
            raise RuntimeError(
                f"OpenAI API error {resp.status_code}: {err_msg}"
            )

        data = resp.json()

        # Extract image bytes — response format varies by model
        image_entry = data["data"][0]
        if "b64_json" in image_entry:
            image_bytes = base64.b64decode(image_entry["b64_json"])
        elif "url" in image_entry:
            # Some models return a URL instead of base64
            with httpx.Client(timeout=self._timeout) as client:
                img_resp = client.get(image_entry["url"])
                img_resp.raise_for_status()
                image_bytes = img_resp.content
        else:
            raise RuntimeError(
                f"Unexpected response format: {list(image_entry.keys())}"
            )

        revised_prompt = image_entry.get("revised_prompt", prompt)

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
                "size_requested": payload.get("size"),
                "revised_prompt": revised_prompt,
            },
        )
