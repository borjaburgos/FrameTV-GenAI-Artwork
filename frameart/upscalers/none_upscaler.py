"""No-op upscaler — uses Pillow's LANCZOS resampling only."""

from __future__ import annotations

import io
import logging

from PIL import Image

from frameart.upscalers.base import Upscaler

logger = logging.getLogger(__name__)


class NoneUpscaler(Upscaler):
    """Upscale using Pillow's built-in LANCZOS resampling.

    This is the simplest approach — no external model or service required.
    Quality is acceptable for moderate upscaling (2-3x) but will be soft
    for larger factors.
    """

    @property
    def name(self) -> str:
        return "none"

    def upscale(self, image_bytes: bytes, target_width: int, target_height: int) -> bytes:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        if w >= target_width and h >= target_height:
            logger.info(
                "Image already >= target (%dx%d >= %dx%d), no upscale needed",
                w, h, target_width, target_height,
            )
            return image_bytes

        logger.info("Pillow LANCZOS upscale %dx%d -> %dx%d", w, h, target_width, target_height)
        img = img.resize((target_width, target_height), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
