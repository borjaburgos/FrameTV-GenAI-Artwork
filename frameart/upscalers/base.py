"""Base interface for image upscalers."""

from __future__ import annotations

import abc


class Upscaler(abc.ABC):
    """Abstract base class for upscaling adapters."""

    @abc.abstractmethod
    def upscale(self, image_bytes: bytes, target_width: int, target_height: int) -> bytes:
        """Upscale an image to (at least) the target dimensions.

        The returned image should be >= target dimensions. The caller will
        handle any final exact-resize.

        Parameters
        ----------
        image_bytes:
            Source image as raw bytes (PNG or JPEG).
        target_width, target_height:
            Desired minimum output dimensions.

        Returns
        -------
        Upscaled image bytes (PNG).
        """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable upscaler name."""
