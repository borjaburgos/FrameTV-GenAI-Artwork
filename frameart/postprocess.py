"""Post-processing: enforce 16:9 aspect ratio and 3840x2160 resolution.

Pipeline:
1. Aspect ratio correction (smart crop to 16:9)
2. Resolution enforcement (upscale if needed, then exact resize to 4K)
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from statistics import mean, pstdev

from PIL import Image

from frameart.upscalers.base import Upscaler

logger = logging.getLogger(__name__)

TARGET_WIDTH = 3840
TARGET_HEIGHT = 2160
TARGET_RATIO = TARGET_WIDTH / TARGET_HEIGHT  # 16/9 ≈ 1.7778
_MAX_BORDER_FRAC = 0.25
_EXTREME_LUMA = 20
_UNIFORM_STD = 3.0
_REF_DELTA = 10.0


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


def _line_luma_stats(rgb_line: Image.Image) -> tuple[float, float]:
    """Return (mean_luma, std_luma) for a 1px-tall or 1px-wide RGB image."""
    pixels = list(rgb_line.getdata())
    lumas = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels]
    return mean(lumas), pstdev(lumas) if len(lumas) > 1 else 0.0


def _count_uniform_border(
    sample: Image.Image,
    *,
    axis: str,
    from_start: bool,
    max_scan: int,
) -> int:
    """Count uniform black/white border pixels on one side of sample image."""
    if axis == "y":
        line_at = (lambda i: i) if from_start else (lambda i: sample.height - 1 - i)
        first_line = sample.crop((0, line_at(0), sample.width, line_at(0) + 1))
    else:
        line_at = (lambda i: i) if from_start else (lambda i: sample.width - 1 - i)
        first_line = sample.crop((line_at(0), 0, line_at(0) + 1, sample.height))

    ref_mean, ref_std = _line_luma_stats(first_line)
    if ref_std > _UNIFORM_STD:
        return 0
    if not (ref_mean <= _EXTREME_LUMA or ref_mean >= 255 - _EXTREME_LUMA):
        return 0

    count = 0
    for i in range(max_scan):
        idx = line_at(i)
        if axis == "y":
            line = sample.crop((0, idx, sample.width, idx + 1))
        else:
            line = sample.crop((idx, 0, idx + 1, sample.height))
        line_mean, line_std = _line_luma_stats(line)
        if line_std > _UNIFORM_STD:
            break
        if abs(line_mean - ref_mean) > _REF_DELTA:
            break
        count += 1
    return count


def trim_embedded_borders(img: Image.Image) -> tuple[Image.Image, str | None]:
    """Trim uniform black/white bars that may be embedded in source images."""
    w, h = img.size
    if w < 64 or h < 64:
        return img, None

    sample_w = min(320, w)
    sample_h = min(320, h)
    sample = img.resize((sample_w, sample_h), Image.Resampling.BILINEAR)

    max_scan_y = max(1, int(sample_h * _MAX_BORDER_FRAC))
    max_scan_x = max(1, int(sample_w * _MAX_BORDER_FRAC))

    top = _count_uniform_border(sample, axis="y", from_start=True, max_scan=max_scan_y)
    bottom = _count_uniform_border(sample, axis="y", from_start=False, max_scan=max_scan_y)
    left = _count_uniform_border(sample, axis="x", from_start=True, max_scan=max_scan_x)
    right = _count_uniform_border(sample, axis="x", from_start=False, max_scan=max_scan_x)

    if top == bottom == left == right == 0:
        return img, None

    trim_top = int(round(top * h / sample_h))
    trim_bottom = int(round(bottom * h / sample_h))
    trim_left = int(round(left * w / sample_w))
    trim_right = int(round(right * w / sample_w))

    new_left = trim_left
    new_upper = trim_top
    new_right = w - trim_right
    new_lower = h - trim_bottom

    if new_right - new_left < int(w * 0.4) or new_lower - new_upper < int(h * 0.4):
        return img, None

    trimmed = img.crop((new_left, new_upper, new_right, new_lower))
    tw, th = trimmed.size
    step = f"trim_borders_{w}x{h}_to_{tw}x{th}"
    logger.info("Trimmed embedded borders %dx%d -> %dx%d", w, h, tw, th)
    return trimmed, step


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

    # Step 1: Embedded border trim
    img, trim_step = trim_embedded_borders(img)
    if trim_step:
        steps.append(trim_step)

    # Step 2: Aspect ratio
    img, crop_step = enforce_aspect_ratio(img)
    if crop_step:
        steps.append(crop_step)

    # Convert back to bytes for upscaler
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    intermediate_bytes = buf.getvalue()

    # Step 3: Resolution
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
