"""Core pipeline: prompt → generate → postprocess → upload → display.

This module orchestrates the full workflow.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from frameart.artifacts import (
    generate_job_id,
    get_job_dir,
    save_final_image,
    save_metadata,
    save_source_image,
)
from frameart.config import STYLE_PRESETS, MeuralProfile, Settings, TVProfile
from frameart.postprocess import postprocess
from frameart.providers.base import ImageProvider
from frameart.providers.registry import get_provider
from frameart.tv import controller as tv_ctrl
from frameart.upscalers.base import Upscaler
from frameart.upscalers.registry import get_upscaler

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Full result of a pipeline run."""

    job_id: str
    job_dir: Path
    source_path: Path | None = None
    final_path: Path | None = None
    content_id: str | None = None
    tv_switched: bool = False
    meural_displayed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    error: str | None = None


def normalize_prompt(
    prompt: str,
    style: str | None = None,
    auto_aspect_hint: bool = True,
    orientation: str = "landscape",
) -> str:
    """Apply style presets and aspect ratio hints to the prompt.

    Parameters
    ----------
    prompt:
        The user's original prompt text.
    style:
        Optional style preset name (e.g., "abstract", "oil_painting").
    auto_aspect_hint:
        If True, append aspect ratio and composition hints to guide
        the model toward the correct composition.
    orientation:
        ``"landscape"`` (default, 16:9) or ``"portrait"`` (9:16).
    """
    parts = [prompt.strip()]

    if style and style in STYLE_PRESETS:
        parts.append(STYLE_PRESETS[style])
    elif style:
        # Custom style text, use as-is
        parts.append(style)

    if auto_aspect_hint:
        if orientation == "portrait":
            parts.append(
                "9:16 aspect ratio, tall portrait composition, no borders or letterboxing"
            )
        else:
            parts.append(
                "16:9 aspect ratio, wide landscape composition, no borders or letterboxing"
            )

    normalized = ", ".join(parts)
    logger.info("Normalized prompt: %s", normalized)
    return normalized


def _get_provider_instance(
    settings: Settings, provider_name: str | None, model: str | None,
) -> ImageProvider:
    """Resolve and instantiate the image provider."""
    name = provider_name or settings.default_provider
    config = settings.providers.get(name)

    # Override model if specified on CLI
    if model and config:
        config = config.model_copy(update={"model": model})
    elif model:
        from frameart.config import ProviderConfig
        config = ProviderConfig(model=model)

    return get_provider(name, config)


def _get_upscaler_instance(settings: Settings, upscaler_name: str | None) -> Upscaler:
    """Resolve and instantiate the upscaler."""
    name = upscaler_name or settings.default_upscaler
    config = settings.upscalers.get(name)
    return get_upscaler(name, config)


def _resolve_tv_profile(
    settings: Settings, tv_name: str | None, tv_ip: str | None,
) -> TVProfile | None:
    """Resolve a TV profile from name or IP."""
    if tv_name and tv_name in settings.tvs:
        return settings.tvs[tv_name]
    if tv_ip:
        return TVProfile(ip=tv_ip)
    # If there's exactly one TV configured, use it
    if len(settings.tvs) == 1:
        return next(iter(settings.tvs.values()))
    return None


def _run_cleanup(
    profile: TVProfile,
    keep: int,
    *,
    order: str = "oldest_first",
    include_favourites: bool = False,
) -> None:
    """Run TV artwork cleanup, logging but not raising on failure."""
    from frameart.tv.cleanup import cleanup_artworks

    try:
        result = cleanup_artworks(
            profile, keep=keep, order=order, include_favourites=include_favourites,
        )
        if result.error:
            logger.warning("TV cleanup warning: %s", result.error)
        elif result.deleted:
            logger.info(
                "TV cleanup: deleted %d artwork(s), kept %d",
                len(result.deleted), result.kept,
            )
    except Exception as exc:
        logger.warning("TV cleanup failed (non-fatal): %s", exc)


def run_generate(
    settings: Settings,
    prompt: str,
    *,
    style: str | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    upscaler_name: str | None = None,
    negative_prompt: str | None = None,
    seed: int | None = None,
    steps: int | None = None,
    guidance: float | None = None,
) -> PipelineResult:
    """Run the generation + post-processing pipeline (no TV upload).

    Returns a PipelineResult with source and final image paths.
    """
    job_id = generate_job_id()
    job_dir = get_job_dir(settings.data_dir, job_id)
    timings: dict[str, float] = {}
    result = PipelineResult(job_id=job_id, job_dir=job_dir, timings=timings)

    try:
        # 1. Normalize prompt
        t0 = time.monotonic()
        normalized = normalize_prompt(prompt, style, settings.auto_aspect_hint)
        timings["prompt_normalize_ms"] = (time.monotonic() - t0) * 1000

        # 2. Generate image
        provider = _get_provider_instance(settings, provider_name, model)
        logger.info("Generating with provider=%s", provider.name)

        t0 = time.monotonic()
        gen_result = provider.generate(
            normalized,
            width=3840,
            height=2160,
            negative_prompt=negative_prompt,
            seed=seed,
            steps=steps,
            guidance=guidance,
        )
        timings["generation_ms"] = (time.monotonic() - t0) * 1000

        # Save source
        result.source_path = save_source_image(job_dir, gen_result.data)

        # 3. Post-process
        upscaler = _get_upscaler_instance(settings, upscaler_name)

        t0 = time.monotonic()
        pp_result = postprocess(gen_result.data, upscaler)
        timings["postprocess_ms"] = (time.monotonic() - t0) * 1000

        # Save final
        result.final_path = save_final_image(job_dir, pp_result.image_bytes)

        # Build metadata
        result.metadata = {
            "job_id": job_id,
            "prompt_original": prompt,
            "prompt_normalized": normalized,
            "style": style,
            "provider": provider.name,
            "model": model or settings.default_model,
            "source_width": gen_result.width,
            "source_height": gen_result.height,
            "final_width": pp_result.width,
            "final_height": pp_result.height,
            "postprocess_steps": pp_result.steps,
            "upscaler": upscaler.name,
            "timings": timings,
            **gen_result.metadata,
        }
        save_metadata(job_dir, result.metadata)

    except Exception as e:
        result.error = str(e)
        logger.error("Pipeline generate failed: %s", e)

    return result


def run_apply(
    settings: Settings,
    image_path: str | Path,
    *,
    tv_name: str | None = None,
    tv_ip: str | None = None,
    matte: str = "none",
    cleanup_keep: int | None = None,
    cleanup_order: str = "oldest_first",
    cleanup_include_favourites: bool = False,
) -> PipelineResult:
    """Upload an existing image to the TV and switch to it.

    Parameters
    ----------
    image_path:
        Path to the image file to upload.
    tv_name:
        Named TV profile from config.
    tv_ip:
        Direct TV IP address.
    matte:
        Matte style for the Frame TV.
    """
    job_id = generate_job_id()
    job_dir = get_job_dir(settings.data_dir, job_id)
    timings: dict[str, float] = {}
    result = PipelineResult(job_id=job_id, job_dir=job_dir, timings=timings)

    try:
        profile = _resolve_tv_profile(settings, tv_name, tv_ip)
        if profile is None:
            raise RuntimeError(
                "No TV specified. Use --tv or --tv-ip, or configure a TV in config.yaml"
            )

        image_bytes = Path(image_path).read_bytes()

        # Determine file type
        file_type = "PNG"
        if str(image_path).lower().endswith((".jpg", ".jpeg")):
            file_type = "JPEG"

        # Upload
        t0 = time.monotonic()
        upload_result = tv_ctrl.upload_image(profile, image_bytes, file_type=file_type, matte=matte)
        timings["upload_ms"] = (time.monotonic() - t0) * 1000

        if not upload_result.success:
            raise RuntimeError(f"Upload failed: {upload_result.error}")

        result.content_id = upload_result.content_id

        # Switch
        t0 = time.monotonic()
        switched = tv_ctrl.switch_art(profile, upload_result.content_id)
        timings["switch_ms"] = (time.monotonic() - t0) * 1000
        result.tv_switched = switched

        result.metadata = {
            "job_id": job_id,
            "image_path": str(image_path),
            "content_id": upload_result.content_id,
            "tv_ip": profile.ip,
            "tv_switched": switched,
            "matte": matte,
            "timings": timings,
        }
        save_metadata(job_dir, result.metadata)

        # Optional cleanup of old artworks on the TV
        if cleanup_keep is not None:
            _run_cleanup(
                profile, cleanup_keep,
                order=cleanup_order,
                include_favourites=cleanup_include_favourites,
            )

    except Exception as e:
        result.error = str(e)
        logger.error("Pipeline apply failed: %s", e)

    return result


def run_generate_and_apply(
    settings: Settings,
    prompt: str,
    *,
    style: str | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    upscaler_name: str | None = None,
    negative_prompt: str | None = None,
    seed: int | None = None,
    steps: int | None = None,
    guidance: float | None = None,
    tv_name: str | None = None,
    tv_ip: str | None = None,
    matte: str = "none",
    no_upload: bool = False,
    no_switch: bool = False,
    cleanup_keep: int | None = None,
    cleanup_order: str = "oldest_first",
    cleanup_include_favourites: bool = False,
) -> PipelineResult:
    """Full pipeline: generate → postprocess → upload → switch display."""
    # Generate + postprocess
    result = run_generate(
        settings,
        prompt,
        style=style,
        provider_name=provider_name,
        model=model,
        upscaler_name=upscaler_name,
        negative_prompt=negative_prompt,
        seed=seed,
        steps=steps,
        guidance=guidance,
    )

    if result.error or not result.final_path:
        return result

    if no_upload:
        logger.info("--no-upload: skipping TV upload")
        return result

    # Upload + switch
    profile = _resolve_tv_profile(settings, tv_name, tv_ip)
    if profile is None:
        result.error = "No TV specified. Use --tv or --tv-ip, or configure a TV in config.yaml"
        logger.error(result.error)
        return result

    image_bytes = result.final_path.read_bytes()
    file_type = "PNG"

    t0 = time.monotonic()
    upload_result = tv_ctrl.upload_image(profile, image_bytes, file_type=file_type, matte=matte)
    result.timings["upload_ms"] = (time.monotonic() - t0) * 1000

    if not upload_result.success:
        result.error = f"Upload failed: {upload_result.error}"
        logger.error(result.error)
        return result

    result.content_id = upload_result.content_id

    if no_switch:
        logger.info("--no-switch: skipping art switch")
    else:
        t0 = time.monotonic()
        result.tv_switched = tv_ctrl.switch_art(profile, upload_result.content_id)
        result.timings["switch_ms"] = (time.monotonic() - t0) * 1000

    # Update metadata with TV info
    result.metadata.update({
        "content_id": result.content_id,
        "tv_ip": profile.ip,
        "tv_switched": result.tv_switched,
        "matte": matte,
    })
    result.metadata["timings"] = result.timings
    save_metadata(result.job_dir, result.metadata)

    # Optional cleanup of old artworks on the TV
    if cleanup_keep is not None:
        _run_cleanup(
            profile, cleanup_keep,
            order=cleanup_order,
            include_favourites=cleanup_include_favourites,
        )

    return result


# ---------------------------------------------------------------------------
# Meural pipeline
# ---------------------------------------------------------------------------

def _resolve_meural_profile(
    settings: Settings, meural_name: str | None, meural_ip: str | None,
) -> MeuralProfile | None:
    """Resolve a Meural profile from name or IP."""
    if meural_name and meural_name in settings.meurals:
        return settings.meurals[meural_name]
    if meural_ip:
        return MeuralProfile(ip=meural_ip)
    if len(settings.meurals) == 1:
        return next(iter(settings.meurals.values()))
    return None


def _meural_target_dims(orientation: str) -> tuple[int, int]:
    """Return (width, height) for the Meural's orientation."""
    if orientation == "portrait":
        return (1080, 1920)
    return (1920, 1080)


def run_meural_generate(
    settings: Settings,
    prompt: str,
    *,
    style: str | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    upscaler_name: str | None = None,
    negative_prompt: str | None = None,
    seed: int | None = None,
    steps: int | None = None,
    guidance: float | None = None,
    orientation: str = "vertical",
) -> PipelineResult:
    """Generate an image sized for a Meural canvas (no device upload).

    Parameters
    ----------
    orientation:
        ``"vertical"`` (portrait, 1080x1920) or ``"horizontal"`` (landscape, 1920x1080).
    """
    orient_key = "portrait" if orientation == "vertical" else "landscape"
    target_w, target_h = _meural_target_dims(orient_key)

    job_id = generate_job_id()
    job_dir = get_job_dir(settings.data_dir, job_id)
    timings: dict[str, float] = {}
    result = PipelineResult(job_id=job_id, job_dir=job_dir, timings=timings)

    try:
        t0 = time.monotonic()
        normalized = normalize_prompt(
            prompt, style, settings.auto_aspect_hint, orientation=orient_key,
        )
        timings["prompt_normalize_ms"] = (time.monotonic() - t0) * 1000

        provider = _get_provider_instance(settings, provider_name, model)
        logger.info("Generating with provider=%s for Meural (%s)", provider.name, orientation)

        t0 = time.monotonic()
        gen_result = provider.generate(
            normalized,
            width=target_w,
            height=target_h,
            negative_prompt=negative_prompt,
            seed=seed,
            steps=steps,
            guidance=guidance,
        )
        timings["generation_ms"] = (time.monotonic() - t0) * 1000

        result.source_path = save_source_image(job_dir, gen_result.data)

        upscaler = _get_upscaler_instance(settings, upscaler_name)

        t0 = time.monotonic()
        pp_result = postprocess(
            gen_result.data, upscaler,
            target_width=target_w, target_height=target_h,
        )
        timings["postprocess_ms"] = (time.monotonic() - t0) * 1000

        result.final_path = save_final_image(job_dir, pp_result.image_bytes)

        result.metadata = {
            "job_id": job_id,
            "prompt_original": prompt,
            "prompt_normalized": normalized,
            "style": style,
            "provider": provider.name,
            "model": model or settings.default_model,
            "source_width": gen_result.width,
            "source_height": gen_result.height,
            "final_width": pp_result.width,
            "final_height": pp_result.height,
            "postprocess_steps": pp_result.steps,
            "upscaler": upscaler.name,
            "target_device": "meural",
            "orientation": orientation,
            "timings": timings,
            **gen_result.metadata,
        }
        save_metadata(job_dir, result.metadata)

    except Exception as e:
        result.error = str(e)
        logger.error("Meural generate pipeline failed: %s", e)

    return result


def run_meural_apply(
    settings: Settings,
    image_path: str | Path,
    *,
    meural_name: str | None = None,
    meural_ip: str | None = None,
    duration: int = 0,
) -> PipelineResult:
    """Upload an existing image to the Meural and display it."""
    from frameart.meural import controller as meural_ctrl

    job_id = generate_job_id()
    job_dir = get_job_dir(settings.data_dir, job_id)
    timings: dict[str, float] = {}
    result = PipelineResult(job_id=job_id, job_dir=job_dir, timings=timings)

    try:
        profile = _resolve_meural_profile(settings, meural_name, meural_ip)
        if profile is None:
            raise RuntimeError(
                "No Meural specified. Use --meural or --meural-ip, "
                "or configure a Meural in config.yaml"
            )

        image_bytes = Path(image_path).read_bytes()

        t0 = time.monotonic()
        display_result = meural_ctrl.display_image(
            profile, image_bytes, duration=duration,
        )
        timings["display_ms"] = (time.monotonic() - t0) * 1000

        if not display_result.success:
            raise RuntimeError(f"Display failed: {display_result.error}")

        result.meural_displayed = True
        result.metadata = {
            "job_id": job_id,
            "image_path": str(image_path),
            "meural_ip": profile.ip,
            "meural_displayed": True,
            "duration": duration,
            "timings": timings,
        }
        save_metadata(job_dir, result.metadata)

    except Exception as e:
        result.error = str(e)
        logger.error("Meural apply pipeline failed: %s", e)

    return result


def run_meural_generate_and_apply(
    settings: Settings,
    prompt: str,
    *,
    style: str | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    upscaler_name: str | None = None,
    negative_prompt: str | None = None,
    seed: int | None = None,
    steps: int | None = None,
    guidance: float | None = None,
    meural_name: str | None = None,
    meural_ip: str | None = None,
    orientation: str = "vertical",
    duration: int = 0,
    no_upload: bool = False,
) -> PipelineResult:
    """Full Meural pipeline: generate → postprocess → display."""
    from frameart.meural import controller as meural_ctrl

    result = run_meural_generate(
        settings,
        prompt,
        style=style,
        provider_name=provider_name,
        model=model,
        upscaler_name=upscaler_name,
        negative_prompt=negative_prompt,
        seed=seed,
        steps=steps,
        guidance=guidance,
        orientation=orientation,
    )

    if result.error or not result.final_path:
        return result

    if no_upload:
        logger.info("--no-upload: skipping Meural display")
        return result

    profile = _resolve_meural_profile(settings, meural_name, meural_ip)
    if profile is None:
        result.error = (
            "No Meural specified. Use --meural or --meural-ip, "
            "or configure a Meural in config.yaml"
        )
        logger.error(result.error)
        return result

    image_bytes = result.final_path.read_bytes()

    t0 = time.monotonic()
    display_result = meural_ctrl.display_image(
        profile, image_bytes, duration=duration,
    )
    result.timings["display_ms"] = (time.monotonic() - t0) * 1000

    if not display_result.success:
        result.error = f"Display failed: {display_result.error}"
        logger.error(result.error)
        return result

    result.meural_displayed = True

    result.metadata.update({
        "meural_ip": profile.ip,
        "meural_displayed": True,
        "orientation": orientation,
        "duration": duration,
    })
    result.metadata["timings"] = result.timings
    save_metadata(result.job_dir, result.metadata)

    return result
