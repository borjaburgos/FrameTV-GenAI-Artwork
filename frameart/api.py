"""FrameArt HTTP API — FastAPI server for generating and uploading art.

Start with:
    frameart serve
    frameart serve --host 0.0.0.0 --port 8000

Endpoints:
    POST /generate-and-apply  — generate image from prompt, upload to TV
    POST /generate            — generate image only (no TV upload)
    POST /apply               — upload an existing image to the TV
    GET  /tv/status           — check TV connection and art mode
    GET  /jobs                — list recent jobs
    GET  /jobs/{job_id}/image — serve the final image for a job
    GET  /health              — liveness check
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from frameart import __version__
from frameart.config import STYLE_PRESETS, load_settings

logger = logging.getLogger(__name__)

app = FastAPI(
    title="FrameArt",
    version=__version__,
    description="Generate AI artwork and display it on Samsung Frame TVs.",
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    """Request body for image generation."""

    prompt: str = Field(..., description="Text description of the image to generate.")
    style: str | None = Field(None, description="Style preset name or custom style text.")
    provider: str | None = Field(None, description="Image provider (openai, ollama, etc.).")
    model: str | None = Field(None, description="Provider-specific model ID.")
    upscaler: str | None = Field(None, description="Upscaler to use.")
    negative_prompt: str | None = Field(None, description="Negative prompt (if supported).")
    seed: int | None = Field(None, description="Deterministic seed (if supported).")
    steps: int | None = Field(None, description="Diffusion steps (if supported).")
    guidance: float | None = Field(None, description="Guidance scale (if supported).")


class GenerateAndApplyRequest(GenerateRequest):
    """Request body for generate + upload + switch."""

    tv: str | None = Field(None, description="TV profile name from config.")
    tv_ip: str | None = Field(None, description="TV IP address (overrides profile).")
    matte: str = Field("none", description="Matte style (e.g., modern_black, none).")
    no_switch: bool = Field(False, description="Upload but don't switch displayed art.")


class ApplyRequest(BaseModel):
    """Request body for uploading an existing image to the TV."""

    image_path: str = Field(..., description="Path to the image file to upload.")
    tv: str | None = Field(None, description="TV profile name from config.")
    tv_ip: str | None = Field(None, description="TV IP address.")
    matte: str = Field("none", description="Matte style.")


class JobResponse(BaseModel):
    """Response for pipeline operations."""

    job_id: str
    job_dir: str
    source_path: str | None = None
    final_path: str | None = None
    content_id: str | None = None
    tv_switched: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    timings: dict[str, float] = Field(default_factory=dict)
    error: str | None = None


class TVStatusResponse(BaseModel):
    """Response for TV status check."""

    reachable: bool
    art_mode_supported: bool = False
    art_mode_on: bool = False
    current_artwork: str | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    """Response for health check."""

    status: str = "ok"
    version: str = __version__


class JobSummary(BaseModel):
    """Summary of a single job for listing."""

    job_id: str
    prompt: str | None = None
    provider: str | None = None
    content_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings():
    """Load settings (cached after first call via lru_cache in config)."""
    return load_settings()


def _pipeline_result_to_response(result) -> JobResponse:
    """Convert a PipelineResult dataclass to a JSON-serialisable response."""
    return JobResponse(
        job_id=result.job_id,
        job_dir=str(result.job_dir),
        source_path=str(result.source_path) if result.source_path else None,
        final_path=str(result.final_path) if result.final_path else None,
        content_id=result.content_id,
        tv_switched=result.tv_switched,
        metadata=result.metadata,
        timings=result.timings,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health():
    """Liveness check."""
    return HealthResponse()


@app.get("/styles", response_model=dict[str, str])
def list_styles():
    """List available style presets."""
    return STYLE_PRESETS


@app.post("/generate", response_model=JobResponse)
def generate(req: GenerateRequest):
    """Generate an image from a text prompt (no TV upload)."""
    from frameart.pipeline import run_generate

    settings = _settings()
    result = run_generate(
        settings,
        req.prompt,
        style=req.style,
        provider_name=req.provider,
        model=req.model,
        upscaler_name=req.upscaler,
        negative_prompt=req.negative_prompt,
        seed=req.seed,
        steps=req.steps,
        guidance=req.guidance,
    )
    resp = _pipeline_result_to_response(result)
    if result.error:
        raise HTTPException(status_code=500, detail=resp.model_dump())
    return resp


@app.post("/generate-and-apply", response_model=JobResponse)
def generate_and_apply(req: GenerateAndApplyRequest):
    """Generate an image, upload it to the Frame TV, and switch display."""
    from frameart.pipeline import run_generate_and_apply

    settings = _settings()
    result = run_generate_and_apply(
        settings,
        req.prompt,
        style=req.style,
        provider_name=req.provider,
        model=req.model,
        upscaler_name=req.upscaler,
        negative_prompt=req.negative_prompt,
        seed=req.seed,
        steps=req.steps,
        guidance=req.guidance,
        tv_name=req.tv,
        tv_ip=req.tv_ip,
        matte=req.matte,
        no_switch=req.no_switch,
    )
    resp = _pipeline_result_to_response(result)
    if result.error:
        raise HTTPException(status_code=500, detail=resp.model_dump())
    return resp


@app.post("/apply", response_model=JobResponse)
def apply_image(req: ApplyRequest):
    """Upload an existing image to the Frame TV and switch to it."""
    from frameart.pipeline import run_apply

    settings = _settings()
    result = run_apply(
        settings,
        req.image_path,
        tv_name=req.tv,
        tv_ip=req.tv_ip,
        matte=req.matte,
    )
    resp = _pipeline_result_to_response(result)
    if result.error:
        raise HTTPException(status_code=500, detail=resp.model_dump())
    return resp


@app.get("/tv/status", response_model=TVStatusResponse)
def tv_status(
    tv: str | None = Query(None, description="TV profile name from config."),
    tv_ip: str | None = Query(None, description="TV IP address."),
):
    """Check the status of a Frame TV."""
    from frameart.config import TVProfile
    from frameart.tv.controller import get_status

    settings = _settings()
    profile = None
    if tv and tv in settings.tvs:
        profile = settings.tvs[tv]
    elif tv_ip:
        profile = TVProfile(ip=tv_ip)
    elif len(settings.tvs) == 1:
        profile = next(iter(settings.tvs.values()))

    if profile is None:
        raise HTTPException(
            status_code=400,
            detail="No TV specified. Pass ?tv=<name> or ?tv_ip=<ip>, "
            "or configure exactly one TV in config.yaml.",
        )

    status = get_status(profile)
    return TVStatusResponse(
        reachable=status.reachable,
        art_mode_supported=status.art_mode_supported,
        art_mode_on=status.art_mode_on,
        current_artwork=status.current_artwork,
        error=status.error,
    )


@app.get("/jobs", response_model=list[JobSummary])
def list_jobs(limit: int = Query(20, ge=1, le=200, description="Max jobs to return.")):
    """List recent generated jobs."""
    import json as _json

    settings = _settings()
    artifacts_dir = settings.data_dir / "artifacts"
    if not artifacts_dir.exists():
        return []

    meta_files = sorted(artifacts_dir.rglob("meta.json"), reverse=True)[:limit]
    jobs: list[JobSummary] = []
    for meta_path in meta_files:
        try:
            meta = _json.loads(meta_path.read_text())
            jobs.append(
                JobSummary(
                    job_id=meta.get("job_id", meta_path.parent.name),
                    prompt=meta.get("prompt_original"),
                    provider=meta.get("provider"),
                    content_id=meta.get("content_id"),
                )
            )
        except Exception:
            jobs.append(JobSummary(job_id=meta_path.parent.name))
    return jobs


@app.get("/jobs/{job_id}/image")
def get_job_image(job_id: str):
    """Serve the final processed image for a job."""
    settings = _settings()
    artifacts_dir = settings.data_dir / "artifacts"

    # Search date-structured dirs for the job
    matches = list(artifacts_dir.rglob(f"{job_id}/final.png"))
    if not matches:
        raise HTTPException(status_code=404, detail=f"No image found for job {job_id}")
    return FileResponse(matches[0], media_type="image/png")


# ---------------------------------------------------------------------------
# Server entry point (used by `frameart serve`)
# ---------------------------------------------------------------------------


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the uvicorn server."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)
