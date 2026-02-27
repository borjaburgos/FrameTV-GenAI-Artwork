"""Google image generation adapter (Gemini/Imagen-style responses)."""

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


class GoogleProvider(ImageProvider):
    """Generate images via Google's Generative Language API."""

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._config = config or ProviderConfig()
        self._api_key = (
            self._config.api_key
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GOOGLE_AI_API_KEY")
            or ""
        )
        self._base_url = (
            self._config.base_url
            or os.environ.get("GOOGLE_BASE_URL")
            or "https://generativelanguage.googleapis.com/v1beta"
        )
        # Model is fully configurable so users can set whichever image-capable
        # Google model they have access to.
        self._model = self._config.model or "gemini-2.5-flash-image-preview"
        self._timeout = self._config.timeout
        self._extra = self._config.extra or {}

    @property
    def name(self) -> str:
        return "google"

    def _normalized_model_name(self) -> str:
        model = self._model.strip()
        if model.startswith("models/"):
            model = model[len("models/") :]
        return model

    def _build_payload(
        self,
        prompt: str,
        *,
        negative_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Build generateContent payload for image output."""
        prompt_text = prompt
        if negative_prompt:
            prompt_text = f"{prompt}\n\nAvoid: {negative_prompt}"

        response_modalities = self._extra.get("response_modalities")
        if not isinstance(response_modalities, list) or not response_modalities:
            # Many Gemini image-capable models require TEXT+IMAGE together.
            response_modalities = ["TEXT", "IMAGE"]

        generation_config: dict[str, Any] = {
            "responseModalities": response_modalities,
        }

        aspect_ratio = self._extra.get("aspect_ratio")
        if isinstance(aspect_ratio, str) and aspect_ratio:
            generation_config["aspectRatio"] = aspect_ratio

        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
            "generationConfig": generation_config,
        }

        safety_settings = self._extra.get("safety_settings")
        if isinstance(safety_settings, list) and safety_settings:
            payload["safetySettings"] = safety_settings

        return payload

    def _extract_image_part(self, data: dict[str, Any]) -> tuple[bytes, str]:
        """Extract base64 inline image bytes from API response."""
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            raise RuntimeError("Google response missing candidates")

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                inline = part.get("inlineData") or part.get("inline_data")
                if not isinstance(inline, dict):
                    continue
                b64 = inline.get("data")
                if not isinstance(b64, str) or not b64:
                    continue
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                return base64.b64decode(b64), str(mime)

        prompt_feedback = data.get("promptFeedback")
        finish_reasons: list[str] = []
        for candidate in candidates:
            if isinstance(candidate, dict):
                reason = candidate.get("finishReason")
                if isinstance(reason, str) and reason:
                    finish_reasons.append(reason)
        raise RuntimeError(
            "Google returned no image data. "
            f"finish_reasons={finish_reasons or 'n/a'} prompt_feedback={prompt_feedback or 'n/a'}"
        )

    def _list_available_models(self, client: httpx.Client) -> list[str]:
        """Best-effort list of available models supporting generateContent."""
        url = f"{self._base_url.rstrip('/')}/models"
        try:
            resp = client.get(url, params={"key": self._api_key})
            resp.raise_for_status()
            payload = resp.json()
            items = payload.get("models") if isinstance(payload, dict) else []
            models: list[str] = []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    methods = item.get("supportedGenerationMethods")
                    if isinstance(methods, list) and "generateContent" not in methods:
                        continue
                    name = item.get("name")
                    if isinstance(name, str) and name.startswith("models/"):
                        models.append(name[len("models/") :])
            return models
        except Exception:
            return []

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
        del width, height, seed, steps, guidance, kwargs

        if not self._api_key:
            raise RuntimeError(
                "Google API key not set. "
                "Set GOOGLE_API_KEY env var or providers.google.api_key"
            )

        payload = self._build_payload(prompt, negative_prompt=negative_prompt)
        model_name = self._normalized_model_name()
        url = f"{self._base_url.rstrip('/')}/models/{model_name}:generateContent"
        logger.info("Google generate: model=%s", model_name)

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                url,
                params={"key": self._api_key},
                headers={"Content-Type": "application/json"},
                json=payload,
            )

            # Compatibility retry: if IMAGE-only was rejected, retry with TEXT+IMAGE.
            if resp.status_code == 400:
                try:
                    err_payload = resp.json()
                except Exception:
                    err_payload = {}
                err_msg = (
                    err_payload.get("error", {}).get("message", "")
                    if isinstance(err_payload, dict)
                    else ""
                )
                modalities = payload.get("generationConfig", {}).get("responseModalities")
                if (
                    isinstance(modalities, list)
                    and modalities == ["IMAGE"]
                    and isinstance(err_msg, str)
                    and "response modalities" in err_msg.lower()
                ):
                    retry_payload = dict(payload)
                    retry_cfg = dict(payload.get("generationConfig", {}))
                    retry_cfg["responseModalities"] = ["TEXT", "IMAGE"]
                    retry_payload["generationConfig"] = retry_cfg
                    resp = client.post(
                        url,
                        params={"key": self._api_key},
                        headers={"Content-Type": "application/json"},
                        json=retry_payload,
                    )

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            if resp.status_code == 404:
                with httpx.Client(timeout=self._timeout) as client:
                    available = self._list_available_models(client)
                hint = (
                    f" Available models for this key: {', '.join(available[:10])}"
                    if available
                    else " Could not list available models for this key."
                )
                raise RuntimeError(
                    "Google model "
                    f"'{model_name}' not found or unsupported for generateContent.{hint}"
                )
            raise RuntimeError(f"Google API error {resp.status_code}: {body}")

        data = resp.json()
        image_bytes, mime_type = self._extract_image_part(data)

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        return GeneratedImage(
            data=image_bytes,
            mime_type=mime_type,
            width=w,
            height=h,
            metadata={
                "provider": self.name,
                "model": model_name,
                "base_url": self._base_url,
            },
        )
