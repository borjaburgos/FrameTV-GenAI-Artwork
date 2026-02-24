"""FrameArt HTTP API — FastAPI server for generating and uploading art.

Start with::

    frameart serve
    frameart serve --host 0.0.0.0 --port 8000

Endpoints (sync):
    POST /generate              — generate image only (no TV upload)
    POST /generate-and-apply    — generate, upload to TV, switch display
    POST /apply                 — upload an existing image to the TV

Endpoints (async — return immediately, poll for results):
    POST /async/generate            — returns {job_id, status}
    POST /async/generate-and-apply  — same, with TV upload
    POST /async/apply               — same, upload-only
    GET  /jobs/{job_id}/status      — poll job progress and result

TV and gallery:
    GET  /tv/status             — check TV connection and art mode
    GET  /tv/discover           — auto-discover Samsung TVs via SSDP
    GET  /tv/configured         — list pre-configured TVs from config
    GET  /tv/art                — list artworks on TV (deduplicated, with favourites)
    POST /tv/art/delete         — delete artworks (skips favourites by default)
    POST /tv/art/matte          — change matte on an artwork
    GET  /tv/mattes             — list matte styles supported by the TV
    GET  /jobs                  — list recent jobs (artifacts on disk)
    POST /jobs/delete           — delete generated jobs from host artifacts
    GET  /jobs/{job_id}/image   — serve the final processed image
    POST /jobs/{job_id}/apply   — upload a previously generated job to TV

Misc:
    GET  /                      — web UI
    GET  /styles                — available style presets
    GET  /health                — liveness check
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

import frameart.public_domain as public_domain
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
    matte: str = Field(
        "none",
        description="Matte style (e.g., none, shadowbox_polar, shadowbox_noir).",
    )
    no_switch: bool = Field(False, description="Upload but don't switch displayed art.")


class ApplyRequest(BaseModel):
    """Request body for uploading an existing image to the TV."""

    image_path: str = Field(..., description="Path to the image file to upload.")
    tv: str | None = Field(None, description="TV profile name from config.")
    tv_ip: str | None = Field(None, description="TV IP address.")
    matte: str = Field(
        "none",
        description="Matte style (run 'tv matte-list' to see options).",
    )


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


class AsyncJobResponse(BaseModel):
    """Response for async job submission."""

    job_id: str
    status: str


class AsyncJobDetail(BaseModel):
    """Detailed status of an async job."""

    job_id: str
    status: str
    request: dict[str, Any] = Field(default_factory=dict)
    result: JobResponse | None = None
    error: str | None = None


class TVStatusResponse(BaseModel):
    """Response for TV status check."""

    reachable: bool
    art_mode_supported: bool = False
    art_mode_on: bool = False
    current_artwork: str | None = None
    error: str | None = None


class DiscoveredTVResponse(BaseModel):
    """A discovered Samsung TV."""

    ip: str
    name: str
    model: str
    frame_tv: bool


class TVArtItem(BaseModel):
    """A single artwork on the TV."""

    content_id: str
    is_favourite: bool = False


class DeleteArtRequest(BaseModel):
    """Request body for deleting artworks from the TV."""

    content_ids: list[str] = Field(..., description="Content IDs to delete.")
    tv: str | None = Field(None, description="TV profile name from config.")
    tv_ip: str | None = Field(None, description="TV IP address.")
    include_favorites: bool = Field(
        False, description="Also delete favourited artworks (skipped by default)."
    )


class DeleteArtResponse(BaseModel):
    """Response for TV art deletion."""

    deleted: list[str] = Field(default_factory=list)
    skipped_favorites: list[str] = Field(default_factory=list)
    error: str | None = None


class ChangeMatteRequest(BaseModel):
    """Request body for changing the matte on a TV artwork."""

    content_id: str = Field(..., description="Content ID of the artwork.")
    matte_id: str = Field(..., description="Matte ID to apply (see GET /tv/mattes).")
    tv: str | None = Field(None, description="TV profile name from config.")
    tv_ip: str | None = Field(None, description="TV IP address.")


class ConfiguredTVResponse(BaseModel):
    """A pre-configured TV from config.yaml."""

    name: str
    ip: str
    port: int = 8002


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


class DeleteJobsRequest(BaseModel):
    """Request body for deleting locally stored generated jobs."""

    job_ids: list[str] = Field(..., description="Job IDs to delete from host artifacts.")


class DeleteJobsResponse(BaseModel):
    """Response for host artifact deletion."""

    deleted: list[str] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)


class PublicDomainArtwork(BaseModel):
    """A normalized public-domain artwork entry."""

    source: str
    artwork_id: str
    title: str
    artist: str | None = None
    date: str | None = None
    image_url: str
    thumbnail_url: str | None = None
    license: str | None = None
    attribution: str | None = None
    source_url: str | None = None
    is_public_domain: bool = True


class PublicDomainApplyRequest(BaseModel):
    """Request body for downloading and displaying public-domain artwork."""

    source: str = Field(..., description="Public domain source: met or aic.")
    artwork_id: str = Field(..., description="Provider-specific artwork ID.")
    tv: str | None = Field(None, description="TV profile name from config.")
    tv_ip: str | None = Field(None, description="TV IP address.")
    matte: str = Field("none", description="Matte style for upload.")


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
# Routes — synchronous
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


@app.get("/tv/discover", response_model=list[DiscoveredTVResponse])
def tv_discover(
    timeout: float = Query(4.0, ge=1, le=30, description="SSDP timeout in seconds."),
    frame_only: bool = Query(False, description="Only return Frame TVs."),
):
    """Auto-discover Samsung TVs on the local network via SSDP."""
    from frameart.tv.discovery import discover

    tvs = discover(timeout=timeout, frame_only=frame_only)
    return [
        DiscoveredTVResponse(ip=tv.ip, name=tv.name, model=tv.model, frame_tv=tv.frame_tv)
        for tv in tvs
    ]


# ---------------------------------------------------------------------------
# Routes — TV art management
# ---------------------------------------------------------------------------


def _resolve_tv_profile(tv: str | None, tv_ip: str | None):
    """Resolve a TVProfile from query params / request body, or raise 400."""
    from frameart.config import TVProfile

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
    return profile


@app.get("/tv/art", response_model=list[TVArtItem])
def tv_list_art(
    tv: str | None = Query(None, description="TV profile name from config."),
    tv_ip: str | None = Query(None, description="TV IP address."),
):
    """List artworks on the Frame TV (deduplicated, with favourite flag)."""
    from frameart.tv.controller import list_art_deduplicated

    profile = _resolve_tv_profile(tv, tv_ip)
    artworks = list_art_deduplicated(profile)
    return [
        TVArtItem(
            content_id=a.get("content_id", "unknown"),
            is_favourite=a.get("is_favourite", False),
        )
        for a in artworks
    ]


@app.get("/tv/art/thumbnail")
def tv_art_thumbnail(
    content_id: str = Query(..., description="Artwork content ID."),
    tv: str | None = Query(None, description="TV profile name from config."),
    tv_ip: str | None = Query(None, description="TV IP address."),
):
    """Fetch thumbnail bytes for an artwork on the Frame TV."""
    from frameart.tv.controller import get_art_thumbnail

    profile = _resolve_tv_profile(tv, tv_ip)
    thumbnail = get_art_thumbnail(profile, content_id)
    if thumbnail is None:
        raise HTTPException(status_code=404, detail="Thumbnail not available.")

    return Response(content=thumbnail, media_type="image/jpeg")


@app.post("/tv/art/delete", response_model=DeleteArtResponse)
def tv_delete_art(req: DeleteArtRequest):
    """Delete artworks from the Frame TV.

    Favourited artworks are skipped by default.  Set ``include_favorites``
    to ``true`` to delete them as well.
    """
    from frameart.tv.controller import delete_art, list_art_deduplicated

    profile = _resolve_tv_profile(req.tv, req.tv_ip)
    ids = list(req.content_ids)
    skipped: list[str] = []

    if not req.include_favorites:
        try:
            artworks = list_art_deduplicated(profile)
            fav_ids = {a["content_id"] for a in artworks if a.get("is_favourite")}
        except Exception:
            fav_ids = set()

        skipped = [cid for cid in ids if cid in fav_ids]
        ids = [cid for cid in ids if cid not in fav_ids]

    if not ids:
        return DeleteArtResponse(deleted=[], skipped_favorites=skipped)

    if delete_art(profile, ids):
        return DeleteArtResponse(deleted=ids, skipped_favorites=skipped)

    raise HTTPException(
        status_code=500,
        detail=DeleteArtResponse(
            deleted=[], skipped_favorites=skipped, error="Failed to delete artwork(s)."
        ).model_dump(),
    )


@app.post("/tv/art/matte")
def tv_change_matte(req: ChangeMatteRequest):
    """Change the matte/frame style on an artwork already on the TV."""
    from frameart.tv.controller import change_matte

    profile = _resolve_tv_profile(req.tv, req.tv_ip)
    if change_matte(profile, req.content_id, req.matte_id):
        return {"ok": True, "content_id": req.content_id, "matte_id": req.matte_id}
    raise HTTPException(status_code=500, detail="Failed to change matte.")


@app.get("/tv/mattes")
def tv_mattes(
    tv: str | None = Query(None, description="TV profile name from config."),
    tv_ip: str | None = Query(None, description="TV IP address."),
):
    """List matte styles supported by the TV."""
    from frameart.tv.controller import get_matte_list

    profile = _resolve_tv_profile(tv, tv_ip)
    return get_matte_list(profile)


@app.get("/tv/configured", response_model=list[ConfiguredTVResponse])
def tv_configured():
    """List TVs pre-configured in config.yaml."""
    settings = _settings()
    return [
        ConfiguredTVResponse(name=name, ip=profile.ip, port=profile.port)
        for name, profile in settings.tvs.items()
    ]


# ---------------------------------------------------------------------------
# Routes — public domain catalog
# ---------------------------------------------------------------------------


@app.get("/catalog/search", response_model=list[PublicDomainArtwork])
def catalog_search(
    source: str = Query(..., description="Catalog source: met or aic."),
    q: str = Query(..., min_length=1, description="Search query."),
    limit: int = Query(20, ge=1, le=50, description="Max results."),
):
    """Search public-domain artwork from supported providers."""
    try:
        items = public_domain.search_artworks(source=source, query=q, limit=limit)
    except ValueError as e:
        logger.warning("Catalog search bad request source=%s q=%r: %s", source, q, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Catalog search upstream failure source=%s q=%r", source, q)
        raise HTTPException(status_code=502, detail=f"Catalog search failed: {e}") from e

    validated: list[PublicDomainArtwork] = []
    dropped = 0
    for item in items:
        try:
            validated.append(PublicDomainArtwork(**item))
        except Exception as e:
            dropped += 1
            logger.warning(
                "Dropping invalid catalog item source=%s q=%r error=%s item=%r",
                source,
                q,
                e,
                item,
            )

    if dropped:
        logger.warning("Catalog search dropped %d invalid items source=%s q=%r", dropped, source, q)

    return validated


@app.post("/catalog/apply", response_model=JobResponse)
def catalog_apply(req: PublicDomainApplyRequest):
    """Download a public-domain artwork and upload it to a TV."""
    from frameart.pipeline import run_import_and_apply

    settings = _settings()
    cache_dir = settings.data_dir / "catalog_cache"

    try:
        image_path, item = public_domain.download_artwork_image(
            req.source,
            req.artwork_id,
            cache_dir,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch artwork: {e}") from e

    result = run_import_and_apply(
        settings,
        str(image_path),
        tv_name=req.tv,
        tv_ip=req.tv_ip,
        matte=req.matte,
        source_metadata=item,
    )

    resp = _pipeline_result_to_response(result)
    if result.error:
        raise HTTPException(status_code=500, detail=resp.model_dump())
    return resp


# ---------------------------------------------------------------------------
# Routes — artifact-based jobs
# ---------------------------------------------------------------------------


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


@app.post("/jobs/delete", response_model=DeleteJobsResponse)
def delete_jobs(req: DeleteJobsRequest):
    """Delete generated job artifacts from the host filesystem."""
    settings = _settings()
    artifacts_dir = settings.data_dir / "artifacts"

    if not artifacts_dir.exists():
        return DeleteJobsResponse(deleted=[], not_found=list(req.job_ids), failed={})

    deleted: list[str] = []
    not_found: list[str] = []
    failed: dict[str, str] = {}

    for job_id in req.job_ids:
        meta_matches = list(artifacts_dir.rglob(f"{job_id}/meta.json"))
        if not meta_matches:
            not_found.append(job_id)
            continue

        job_dirs = {m.parent for m in meta_matches}
        try:
            for job_dir in job_dirs:
                shutil.rmtree(job_dir)
            deleted.append(job_id)
        except Exception as e:
            failed[job_id] = str(e)

    return DeleteJobsResponse(deleted=deleted, not_found=not_found, failed=failed)


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


class JobApplyRequest(BaseModel):
    """Request body for uploading a previously generated job's image to a TV."""

    tv: str | None = Field(None, description="TV profile name from config.")
    tv_ip: str | None = Field(None, description="TV IP address.")
    matte: str = Field(
        "none",
        description="Matte style (e.g., none, shadowbox_polar, shadowbox_noir).",
    )


@app.post("/jobs/{job_id}/apply", response_model=JobResponse)
def apply_job_to_tv(job_id: str, req: JobApplyRequest):
    """Upload a previously generated job's image to a TV."""
    from frameart.pipeline import run_apply

    settings = _settings()
    artifacts_dir = settings.data_dir / "artifacts"
    matches = list(artifacts_dir.rglob(f"{job_id}/final.png"))
    if not matches:
        raise HTTPException(status_code=404, detail=f"No image found for job {job_id}")

    result = run_apply(
        settings,
        str(matches[0]),
        tv_name=req.tv,
        tv_ip=req.tv_ip,
        matte=req.matte,
    )
    resp = _pipeline_result_to_response(result)
    if result.error:
        raise HTTPException(status_code=500, detail=resp.model_dump())
    return resp


# ---------------------------------------------------------------------------
# Routes — async job queue
# ---------------------------------------------------------------------------


@app.get("/jobs/{job_id}/status", response_model=AsyncJobDetail)
def get_job_status(job_id: str):
    """Get the detailed status of an async job."""
    from frameart.jobs import job_store

    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found in async queue")

    result_response = None
    if job.result is not None:
        result_response = _pipeline_result_to_response(job.result)

    return AsyncJobDetail(
        job_id=job.id,
        status=job.status.value,
        request=job.request,
        result=result_response,
        error=job.error,
    )


@app.post("/async/generate", response_model=AsyncJobResponse)
def async_generate(req: GenerateRequest):
    """Submit an image generation job to the background queue."""
    from frameart.artifacts import generate_job_id
    from frameart.jobs import job_store
    from frameart.pipeline import run_generate

    settings = _settings()
    job_id = generate_job_id()

    job_store.submit(
        job_id=job_id,
        func=run_generate,
        args=(settings, req.prompt),
        kwargs={
            "style": req.style,
            "provider_name": req.provider,
            "model": req.model,
            "upscaler_name": req.upscaler,
            "negative_prompt": req.negative_prompt,
            "seed": req.seed,
            "steps": req.steps,
            "guidance": req.guidance,
        },
        request_summary={"type": "generate", "prompt": req.prompt, "style": req.style},
    )

    return AsyncJobResponse(job_id=job_id, status="pending")


@app.post("/async/generate-and-apply", response_model=AsyncJobResponse)
def async_generate_and_apply(req: GenerateAndApplyRequest):
    """Submit a generate-and-apply job to the background queue."""
    from frameart.artifacts import generate_job_id
    from frameart.jobs import job_store
    from frameart.pipeline import run_generate_and_apply

    settings = _settings()
    job_id = generate_job_id()

    job_store.submit(
        job_id=job_id,
        func=run_generate_and_apply,
        args=(settings, req.prompt),
        kwargs={
            "style": req.style,
            "provider_name": req.provider,
            "model": req.model,
            "upscaler_name": req.upscaler,
            "negative_prompt": req.negative_prompt,
            "seed": req.seed,
            "steps": req.steps,
            "guidance": req.guidance,
            "tv_name": req.tv,
            "tv_ip": req.tv_ip,
            "matte": req.matte,
            "no_switch": req.no_switch,
        },
        request_summary={
            "type": "generate-and-apply",
            "prompt": req.prompt,
            "style": req.style,
        },
    )

    return AsyncJobResponse(job_id=job_id, status="pending")


@app.post("/async/apply", response_model=AsyncJobResponse)
def async_apply(req: ApplyRequest):
    """Submit an apply (upload) job to the background queue."""
    from frameart.artifacts import generate_job_id
    from frameart.jobs import job_store
    from frameart.pipeline import run_apply

    settings = _settings()
    job_id = generate_job_id()

    job_store.submit(
        job_id=job_id,
        func=run_apply,
        args=(settings, req.image_path),
        kwargs={
            "tv_name": req.tv,
            "tv_ip": req.tv_ip,
            "matte": req.matte,
        },
        request_summary={"type": "apply", "image_path": req.image_path},
    )

    return AsyncJobResponse(job_id=job_id, status="pending")


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
def web_ui():
    """Serve the FrameArt web UI."""
    index = _STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Web UI not found")
    return HTMLResponse(index.read_text())


# ---------------------------------------------------------------------------
# Server entry point (used by `frameart serve`)
# ---------------------------------------------------------------------------


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the uvicorn server."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)
