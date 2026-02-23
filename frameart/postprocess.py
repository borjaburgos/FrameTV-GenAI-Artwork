"""Post-processing: enforce 16:9 aspect ratio and 3840x2160 resolution.

Pipeline:
1. Aspect ratio correction (smart crop to 16:9)
2. Resolution enforcement (upscale if needed, then exact resize to 4K)
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from PIL import Image

from frameart.upscalers.base import Upscaler

logger = logging.getLogger(__name__)

TARGET_WIDTH = 3840
TARGET_HEIGHT = 2160
TARGET_RATIO = TARGET_WIDTH / TARGET_HEIGHT  # 16/9 ≈ 1.7778


@dataclass
class PostProcessResult:
    """Result of post-processing an image."""

    image_bytes: bytes
    width: int
    height: int
    steps: list[str]


def _compute_crop_box(
    src_w: int, src_h: int, target_ratio: float
) -> tuple[int, int, int, int]:
    """Compute a center-weighted crop box to achieve the target aspect ratio.

    Returns (left, upper, right, lower) for PIL crop.
    """
    src_ratio = src_w / src_h

    if abs(src_ratio - target_ratio) < 0.01:
        return (0, 0, src_w, src_h)

    if src_ratio > target_ratio:
        # Image is wider than 16:9 — crop width
        new_w = int(src_h * target_ratio)
        offset = (src_w - new_w) // 2
        return (offset, 0, offset + new_w, src_h)
    else:
        # Image is taller than 16:9 — crop height
        new_h = int(src_w / target_ratio)
        offset = (src_h - new_h) // 2
        return (0, offset, src_w, offset + new_h)


def enforce_aspect_ratio(img: Image.Image) -> tuple[Image.Image, str | None]:
    """Crop image to exactly 16:9 if needed. Returns (image, step_description)."""
    w, h = img.size
    current_ratio = w / h

    if abs(current_ratio - TARGET_RATIO) < 0.01:
        logger.info("Aspect ratio already 16:9 (%dx%d)", w, h)
        return img, None

    box = _compute_crop_box(w, h, TARGET_RATIO)
    cropped = img.crop(box)
    new_w, new_h = cropped.size
    step = f"crop_{w}x{h}_to_{new_w}x{new_h}"
    logger.info(
        "Cropped %dx%d -> %dx%d (ratio %.4f -> %.4f)",
        w, h, new_w, new_h, current_ratio, new_w / new_h,
    )
    return cropped, step


def enforce_resolution(
    img: Image.Image,
    image_bytes: bytes,
    upscaler: Upscaler,
) -> tuple[Image.Image, list[str]]:
    """Ensure the image is exactly 3840x2160.

    If smaller, use the provided upscaler first, then resize.
    If larger, downscale with LANCZOS.
    """
    w, h = img.size
    steps: list[str] = []

    if w == TARGET_WIDTH and h == TARGET_HEIGHT:
        logger.info("Image already at target resolution %dx%d", w, h)
        return img, steps

    if w < TARGET_WIDTH or h < TARGET_HEIGHT:
        # Upscale needed
        logger.info("Image %dx%d is below 4K, upscaling with %s", w, h, upscaler.name)
        upscaled_bytes = upscaler.upscale(image_bytes, TARGET_WIDTH, TARGET_HEIGHT)
        img = Image.open(io.BytesIO(upscaled_bytes))
        w, h = img.size
        steps.append(f"upscale_{upscaler.name}_to_{w}x{h}")

    # Final exact resize (handles both upscaled-but-not-exact and larger-than-4K)
    if w != TARGET_WIDTH or h != TARGET_HEIGHT:
        logger.info("Final resize %dx%d -> %dx%d (LANCZOS)", w, h, TARGET_WIDTH, TARGET_HEIGHT)
        img = img.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.LANCZOS)
        steps.append(f"resize_to_{TARGET_WIDTH}x{TARGET_HEIGHT}")

    return img, steps


def postprocess(
    image_bytes: bytes,
    upscaler: Upscaler,
) -> PostProcessResult:
    """Run the full post-processing pipeline.

    1. Enforce 16:9 aspect ratio (smart crop)
    2. Enforce 3840x2160 resolution (upscale/downscale)

    Returns PostProcessResult with the final PNG bytes.
    """
    steps: list[str] = []

    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")

    # Step 1: Aspect ratio
    img, crop_step = enforce_aspect_ratio(img)
    if crop_step:
        steps.append(crop_step)

    # Convert back to bytes for upscaler
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    intermediate_bytes = buf.getvalue()

    # Step 2: Resolution
    img, res_steps = enforce_resolution(img, intermediate_bytes, upscaler)
    steps.extend(res_steps)

    # Final output
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    final_bytes = buf.getvalue()

    return PostProcessResult(
        image_bytes=final_bytes,
        width=TARGET_WIDTH,
        height=TARGET_HEIGHT,
        steps=steps,
    )
