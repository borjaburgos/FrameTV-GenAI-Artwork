"""Tests for upscaler registry and NoneUpscaler."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from frameart.upscalers.base import Upscaler
from frameart.upscalers.none_upscaler import NoneUpscaler
from frameart.upscalers.registry import available_upscalers, get_upscaler


def _make_png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), "blue")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestUpscalerRegistry:
    def test_available_upscalers(self):
        upscalers = available_upscalers()
        assert "none" in upscalers
        assert "local_http" in upscalers
        assert "remote_http" in upscalers

    def test_get_none_upscaler(self):
        upscaler = get_upscaler("none")
        assert isinstance(upscaler, Upscaler)
        assert upscaler.name == "none"

    def test_get_unknown_upscaler(self):
        with pytest.raises(KeyError, match="Unknown upscaler"):
            get_upscaler("nonexistent")


class TestNoneUpscaler:
    def test_no_upscale_needed(self):
        img_bytes = _make_png(3840, 2160)
        upscaler = NoneUpscaler()
        result = upscaler.upscale(img_bytes, 3840, 2160)
        # Should return the same bytes (no processing)
        assert result == img_bytes

    def test_upscale_small_image(self):
        img_bytes = _make_png(960, 540)
        upscaler = NoneUpscaler()
        result = upscaler.upscale(img_bytes, 3840, 2160)
        # Result should be a valid image at target size
        img = Image.open(io.BytesIO(result))
        assert img.size == (3840, 2160)

    def test_upscale_preserves_rgb(self):
        img_bytes = _make_png(100, 100)
        upscaler = NoneUpscaler()
        result = upscaler.upscale(img_bytes, 200, 200)
        img = Image.open(io.BytesIO(result))
        assert img.mode == "RGB"
