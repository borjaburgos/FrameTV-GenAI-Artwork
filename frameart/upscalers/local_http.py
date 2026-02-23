"""Local HTTP upscaler — calls a LAN-hosted upscaling service.

Expects a service (e.g., Real-ESRGAN) that accepts POST with an image
and returns the upscaled image.
"""

from __future__ import annotations

import io
import logging
import os

import httpx
from PIL import Image

from frameart.config import UpscalerConfig
from frameart.upscalers.base import Upscaler

logger = logging.getLogger(__name__)


class LocalHTTPUpscaler(Upscaler):
    """Upscale via a local HTTP endpoint (e.g., Real-ESRGAN service)."""

    def __init__(self, config: UpscalerConfig | None = None) -> None:
        self._config = config or UpscalerConfig()
        self._base_url = (
            self._config.base_url
            or os.environ.get("FRAMEART_UPSCALER_URL")
            or "http://localhost:7860"
        )
        self._timeout = self._config.timeout

    @property
    def name(self) -> str:
        return "local_http"

    def upscale(self, image_bytes: bytes, target_width: int, target_height: int) -> bytes:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        if w >= target_width and h >= target_height:
            logger.info("Image already >= target, skipping upscale")
            return image_bytes

        scale_factor = max(target_width / w, target_height / h)
        # Round up to nearest 0.5
        scale_factor = max(2.0, round(scale_factor * 2) / 2)

        logger.info(
            "Local HTTP upscale %dx%d -> ~%.1fx (target %dx%d)",
            w, h, scale_factor, target_width, target_height,
        )

        url = f"{self._base_url}/api/upscale"

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                url,
                files={"image": ("input.png", image_bytes, "image/png")},
                data={"scale": str(scale_factor)},
            )
            resp.raise_for_status()

        result_bytes = resp.content

        # Verify output
        result_img = Image.open(io.BytesIO(result_bytes))
        logger.info("Local upscaler returned %dx%d", result_img.size[0], result_img.size[1])

        return result_bytes
