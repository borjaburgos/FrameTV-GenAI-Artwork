"""Remote HTTP upscaler — calls an external upscaling service."""

from __future__ import annotations

import io
import logging
import os

import httpx
from PIL import Image

from frameart.config import UpscalerConfig
from frameart.upscalers.base import Upscaler

logger = logging.getLogger(__name__)


class RemoteHTTPUpscaler(Upscaler):
    """Upscale via a remote HTTP API (e.g., Freepik, Let's Enhance, etc.)."""

    def __init__(self, config: UpscalerConfig | None = None) -> None:
        self._config = config or UpscalerConfig()
        self._base_url = (
            self._config.base_url
            or os.environ.get("FRAMEART_REMOTE_UPSCALER_URL")
            or ""
        )
        self._api_key = (
            self._config.api_key
            or os.environ.get("FRAMEART_REMOTE_UPSCALER_KEY")
            or ""
        )
        self._timeout = self._config.timeout

    @property
    def name(self) -> str:
        return "remote_http"

    def upscale(self, image_bytes: bytes, target_width: int, target_height: int) -> bytes:
        if not self._base_url:
            raise RuntimeError(
                "Remote upscaler URL not configured. "
                "Set FRAMEART_REMOTE_UPSCALER_URL or configure upscalers.remote_http.base_url"
            )

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        if w >= target_width and h >= target_height:
            logger.info("Image already >= target, skipping upscale")
            return image_bytes

        scale_factor = max(target_width / w, target_height / h)

        logger.info(
            "Remote HTTP upscale %dx%d -> ~%.1fx (target %dx%d)",
            w, h, scale_factor, target_width, target_height,
        )

        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self._base_url}/upscale",
                headers=headers,
                files={"image": ("input.png", image_bytes, "image/png")},
                data={
                    "target_width": str(target_width),
                    "target_height": str(target_height),
                },
            )
            resp.raise_for_status()

        result_bytes = resp.content
        result_img = Image.open(io.BytesIO(result_bytes))
        logger.info("Remote upscaler returned %dx%d", result_img.size[0], result_img.size[1])

        return result_bytes
