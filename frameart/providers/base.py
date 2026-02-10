"""Base interface for image generation providers."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GeneratedImage:
    """Result from an image generation provider."""

    data: bytes
    mime_type: str  # e.g. "image/png"
    width: int
    height: int
    metadata: dict[str, Any] = field(default_factory=dict)


class ImageProvider(abc.ABC):
    """Abstract base class for image generation adapters.

    All adapters must implement ``generate()``.
    """

    @abc.abstractmethod
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
        """Generate an image from a text prompt.

        Parameters
        ----------
        prompt:
            The text prompt describing the desired image.
        width, height:
            Requested output dimensions (provider may not respect these).
        negative_prompt:
            Things to avoid in the image (if supported).
        seed:
            Deterministic seed (if supported).
        steps:
            Number of diffusion steps (if supported).
        guidance:
            Classifier-free guidance scale (if supported).

        Returns
        -------
        GeneratedImage with raw bytes, detected dimensions, and metadata.
        """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""
