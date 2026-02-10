"""Tests for post-processing: aspect ratio enforcement and resolution handling."""

from __future__ import annotations

import io

from PIL import Image

from frameart.postprocess import (
    TARGET_HEIGHT,
    TARGET_RATIO,
    TARGET_WIDTH,
    _compute_crop_box,
    enforce_aspect_ratio,
    enforce_resolution,
    postprocess,
)
from frameart.upscalers.none_upscaler import NoneUpscaler


def _make_image(width: int, height: int, color: str = "red") -> tuple[Image.Image, bytes]:
    """Create a test image and return (PIL Image, bytes)."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return img, buf.getvalue()


# --- Aspect ratio crop box computation ---


class TestComputeCropBox:
    def test_already_16_9(self):
        box = _compute_crop_box(1920, 1080, TARGET_RATIO)
        assert box == (0, 0, 1920, 1080)

    def test_wider_than_16_9(self):
        # 2:1 is wider than 16:9 — should crop width
        box = _compute_crop_box(2000, 1000, TARGET_RATIO)
        left, upper, right, lower = box
        new_w = right - left
        new_h = lower - upper
        assert new_h == 1000
        assert abs(new_w / new_h - TARGET_RATIO) < 0.01
        # Should be center-cropped
        assert left > 0
        assert upper == 0

    def test_taller_than_16_9(self):
        # 1:1 is taller than 16:9 — should crop height
        box = _compute_crop_box(1000, 1000, TARGET_RATIO)
        left, upper, right, lower = box
        new_w = right - left
        new_h = lower - upper
        assert new_w == 1000
        assert abs(new_w / new_h - TARGET_RATIO) < 0.01
        assert upper > 0
        assert left == 0

    def test_exact_4k(self):
        box = _compute_crop_box(3840, 2160, TARGET_RATIO)
        assert box == (0, 0, 3840, 2160)


# --- Aspect ratio enforcement ---


class TestEnforceAspectRatio:
    def test_already_correct(self):
        img, _ = _make_image(1920, 1080)
        result, step = enforce_aspect_ratio(img)
        assert step is None
        assert result.size == (1920, 1080)

    def test_square_to_16_9(self):
        img, _ = _make_image(1000, 1000)
        result, step = enforce_aspect_ratio(img)
        assert step is not None
        w, h = result.size
        assert abs(w / h - TARGET_RATIO) < 0.01

    def test_ultra_wide(self):
        img, _ = _make_image(3000, 1000)
        result, step = enforce_aspect_ratio(img)
        assert step is not None
        w, h = result.size
        assert abs(w / h - TARGET_RATIO) < 0.01

    def test_portrait(self):
        img, _ = _make_image(1000, 2000)
        result, step = enforce_aspect_ratio(img)
        assert step is not None
        w, h = result.size
        assert abs(w / h - TARGET_RATIO) < 0.01


# --- Resolution enforcement ---


class TestEnforceResolution:
    def test_already_4k(self):
        img, img_bytes = _make_image(3840, 2160)
        upscaler = NoneUpscaler()
        result, steps = enforce_resolution(img, img_bytes, upscaler)
        assert result.size == (3840, 2160)
        assert len(steps) == 0

    def test_upscale_small(self):
        img, img_bytes = _make_image(1920, 1080)
        upscaler = NoneUpscaler()
        result, steps = enforce_resolution(img, img_bytes, upscaler)
        assert result.size == (3840, 2160)
        assert len(steps) > 0

    def test_downscale_large(self):
        img, img_bytes = _make_image(7680, 4320)
        upscaler = NoneUpscaler()
        result, steps = enforce_resolution(img, img_bytes, upscaler)
        assert result.size == (3840, 2160)
        assert any("resize" in s for s in steps)


# --- Full post-processing pipeline ---


class TestPostprocess:
    def test_full_pipeline_square(self):
        _, img_bytes = _make_image(1024, 1024)
        upscaler = NoneUpscaler()
        result = postprocess(img_bytes, upscaler)
        assert result.width == TARGET_WIDTH
        assert result.height == TARGET_HEIGHT
        assert len(result.steps) > 0

    def test_full_pipeline_already_4k_16_9(self):
        _, img_bytes = _make_image(3840, 2160)
        upscaler = NoneUpscaler()
        result = postprocess(img_bytes, upscaler)
        assert result.width == TARGET_WIDTH
        assert result.height == TARGET_HEIGHT

    def test_full_pipeline_dall_e_landscape(self):
        # DALL-E 3 returns 1792x1024 — wider than 16:9 (1.75:1 vs 1.78:1)
        _, img_bytes = _make_image(1792, 1024)
        upscaler = NoneUpscaler()
        result = postprocess(img_bytes, upscaler)
        assert result.width == TARGET_WIDTH
        assert result.height == TARGET_HEIGHT

    def test_output_is_valid_png(self):
        _, img_bytes = _make_image(800, 600)
        upscaler = NoneUpscaler()
        result = postprocess(img_bytes, upscaler)
        # Verify the output bytes form a valid image
        img = Image.open(io.BytesIO(result.image_bytes))
        assert img.size == (TARGET_WIDTH, TARGET_HEIGHT)
        assert img.mode == "RGB"
