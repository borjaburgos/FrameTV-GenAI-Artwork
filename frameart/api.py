"""FrameArt HTTP API — FastAPI server for generating and uploading art.

Start with::

    frameart serve
    frameart serve --host 0.0.0.0 --port 8000

Endpoints (sync):
    POST /generate              — generate image only (no TV upload)
    POST /generate-and-apply    — generate, upload to TV, switch display
    POST /upload-and-apply      — upload image bytes from web/mobile and apply to TV
    POST /edit-and-apply        — upload + edit image with prompt, then apply to TV
    POST /jobs/{job_id}/edit-and-apply — edit from existing server artwork
    POST /tv/art/edit-and-apply — edit from artwork currently stored on TV

Endpoints (async — return immediately, poll for results):
    POST /async/generate            — returns {job_id, status}
    POST /async/generate-and-apply  — same, with TV upload
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
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from io import BytesIO
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import AfterValidator, BaseModel, Field, SecretStr, field_validator

import frameart.public_domain as public_domain
from frameart import __version__
from frameart.config import STYLE_PRESETS, ProviderConfig, TVProfile, load_settings
from frameart.jobs import JobQueueFullError
from frameart.providers.registry import available_providers
from frameart.settings_store import read_provider_secrets, update_management_state

logger = logging.getLogger(__name__)

_ALLOWED_UPLOAD_EXTS = {".jpg", ".jpeg", ".png"}
_ALLOWED_UPLOAD_MIME = {"image/jpeg", "image/jpg", "image/png"}
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_MAX_UPLOAD_PIXELS = 50_000_000
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_IDENTIFIER_RE = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_PUBLIC_PATHS = {
    "/",
    "/health",
    "/auth/session",
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
}
_ADMIN_PATHS = {"/jobs/delete", "/tv/art/delete", "/tv/art/matte"}
_ADMIN_PREFIXES = ("/settings",)
_rate_limit_events: dict[str, deque[float]] = defaultdict(deque)
_rate_limit_lock = threading.Lock()


def _strip_nonempty(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be empty")
    return stripped


def _private_tv_ip(value: str) -> str:
    return TVProfile(ip=value).ip


Prompt = Annotated[
    str,
    Field(min_length=1, max_length=4000),
    AfterValidator(_strip_nonempty),
]
PrivateTVIPv4 = Annotated[str, AfterValidator(_private_tv_ip)]
ContentId = Annotated[str, Field(pattern=_IDENTIFIER_RE)]
JobId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]

app = FastAPI(
    title="FrameArt",
    version=__version__,
    description="Generate AI artwork and display it on Samsung Frame TVs.",
)


def _secret_value(value: SecretStr | str | None) -> str | None:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


def _presented_token(request: Request) -> tuple[str | None, bool]:
    """Return a presented API token and whether it came from a cookie."""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip(), False
    header_token = request.headers.get("x-frameart-token")
    if header_token:
        return header_token.strip(), False
    cookie_token = request.cookies.get("frameart_session")
    return (cookie_token, True) if cookie_token else (None, False)


def _token_scopes(settings, token: str | None) -> set[str]:
    if not token:
        return set()
    admin_token = _secret_value(settings.admin_token)
    automation_token = _secret_value(settings.automation_token)
    if admin_token and secrets.compare_digest(token, admin_token):
        return {"read", "control", "admin"}
    if automation_token and secrets.compare_digest(token, automation_token):
        return {"read", "control"}
    return set()


def _required_scope(request: Request) -> str:
    if request.url.path.startswith(_ADMIN_PREFIXES):
        return "admin"
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return "read"
    if request.url.path in _ADMIN_PATHS:
        return "admin"
    return "control"


def _within_rate_limit(key: str, limit: int) -> bool:
    """Apply a small in-process sliding-window limit to mutating requests."""
    now = time.monotonic()
    cutoff = now - 60.0
    with _rate_limit_lock:
        events = _rate_limit_events[key]
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
        return True


@app.middleware("http")
async def authenticate_request(
    request: Request,
    call_next: Callable,
):
    """Enforce scoped tokens when API authentication is enabled."""
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    settings = load_settings()
    if not settings.auth_enabled:
        return await call_next(request)

    token, from_cookie = _presented_token(request)
    scopes = _token_scopes(settings, token)
    required = _required_scope(request)
    if required not in scopes:
        status_code = 401 if not scopes else 403
        return JSONResponse(
            status_code=status_code,
            content={"detail": f"A token with the {required!r} scope is required."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    if from_cookie and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/").split("://")[-1] != request.headers.get("host"):
            return JSONResponse(status_code=403, content={"detail": "Origin check failed."})

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        client_id = f"{next(iter(sorted(scopes)))}:{request.client.host if request.client else '-'}"
        if not _within_rate_limit(client_id, settings.api_rate_limit_per_minute):
            return JSONResponse(
                status_code=429,
                content={"detail": "API rate limit exceeded; retry in one minute."},
                headers={"Retry-After": "60"},
            )

    request.state.frameart_scopes = scopes
    return await call_next(request)


@app.exception_handler(JobQueueFullError)
async def job_queue_full_handler(_request: Request, exc: JobQueueFullError):
    return JSONResponse(
        status_code=429,
        content={"detail": str(exc)},
        headers={"Retry-After": "10"},
    )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    """Request body for image generation."""

    prompt: Prompt = Field(..., description="Text description of the image to generate.")
    style: str | None = Field(
        None, max_length=500, description="Style preset name or custom style text."
    )
    provider: str | None = Field(None, max_length=100, description="Image provider name.")
    model: str | None = Field(None, max_length=200, description="Provider-specific model ID.")
    upscaler: str | None = Field(None, max_length=100, description="Upscaler to use.")
    negative_prompt: str | None = Field(
        None, max_length=4000, description="Negative prompt (if supported)."
    )
    seed: int | None = Field(None, description="Deterministic seed (if supported).")
    steps: int | None = Field(None, ge=1, le=500, description="Diffusion steps.")
    guidance: float | None = Field(None, ge=0, le=100, description="Guidance scale.")


class GenerateAndApplyRequest(GenerateRequest):
    """Request body for generate + upload + switch."""

    tv: str | None = Field(None, max_length=100, description="TV profile name from config.")
    tv_ip: PrivateTVIPv4 | None = Field(None, description="Private TV IP address.")
    matte: str = Field(
        "none", max_length=100,
        description="Matte style (e.g., none, shadowbox_polar, shadowbox_noir).",
    )
    no_switch: bool = Field(False, description="Upload but don't switch displayed art.")


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

    content_ids: list[ContentId] = Field(
        ..., min_length=1, max_length=100, description="Content IDs to delete."
    )
    tv: str | None = Field(None, max_length=100, description="TV profile name from config.")
    tv_ip: PrivateTVIPv4 | None = Field(None, description="Private TV IP address.")
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

    content_id: ContentId = Field(..., description="Content ID of the artwork.")
    matte_id: str = Field(
        ..., min_length=1, max_length=100, pattern=_IDENTIFIER_RE,
        description="Matte ID to apply (see GET /tv/mattes).",
    )
    tv: str | None = Field(None, max_length=100, description="TV profile name from config.")
    tv_ip: PrivateTVIPv4 | None = Field(None, description="Private TV IP address.")


class DisplayArtRequest(BaseModel):
    """Request body for displaying an existing TV artwork by content ID."""

    content_id: ContentId = Field(..., description="Content ID of the artwork to display.")
    tv: str | None = Field(None, max_length=100, description="TV profile name from config.")
    tv_ip: PrivateTVIPv4 | None = Field(None, description="Private TV IP address.")


class ConfiguredTVResponse(BaseModel):
    """A pre-configured TV from config.yaml."""

    name: str
    ip: str
    port: int = 8002


class HealthResponse(BaseModel):
    """Response for health check."""

    status: str = "ok"
    version: str = __version__


class AuthSessionRequest(BaseModel):
    """Token exchange used by the browser UI."""

    token: SecretStr = Field(..., min_length=20, max_length=4096)


class ProviderOption(BaseModel):
    """Configured provider metadata for the web UI."""

    name: str
    is_default: bool = False
    models: list[str] = Field(default_factory=list)
    default_model: str | None = None


class ProvidersResponse(BaseModel):
    """Configured providers and model options."""

    default_provider: str
    providers: list[ProviderOption] = Field(default_factory=list)


ProviderId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ProfileId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
ModelId = Annotated[str, Field(min_length=1, max_length=200)]


class ProviderSettingsRequest(BaseModel):
    """Editable non-secret provider settings plus optional key rotation."""

    base_url: str | None = Field(None, max_length=2048)
    model: str | None = Field(None, max_length=200)
    timeout: int = Field(120, ge=1, le=600)
    models: list[ModelId] = Field(default_factory=list, max_length=100)
    api_key: SecretStr | None = Field(None, min_length=1, max_length=8192)
    clear_api_key: bool = False

    @field_validator("base_url", "model", mode="before")
    @classmethod
    def strip_optional_text(cls, value):
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        return value.rstrip("/")

    @field_validator("models")
    @classmethod
    def normalize_models(cls, value: list[str]) -> list[str]:
        normalized = [model.strip() for model in value if model.strip()]
        return list(dict.fromkeys(normalized))


class ProviderCreateRequest(ProviderSettingsRequest):
    """Create a built-in provider configuration."""

    name: ProviderId


class ManagedProviderResponse(BaseModel):
    """Provider configuration safe to return to an administrator."""

    name: str
    base_url: str | None = None
    model: str | None = None
    timeout: int = 120
    models: list[str] = Field(default_factory=list)
    has_api_key: bool = False
    api_key_source: str | None = None
    is_default: bool = False


class ManagedProvidersResponse(BaseModel):
    """Editable provider configuration and available adapter types."""

    default_provider: str
    default_model: str | None = None
    available_types: list[str] = Field(default_factory=list)
    providers: list[ManagedProviderResponse] = Field(default_factory=list)


class DefaultsSettingsRequest(BaseModel):
    """Default provider/model selection."""

    provider: ProviderId
    model: str | None = Field(None, max_length=200)

    @field_validator("model", mode="before")
    @classmethod
    def strip_model(cls, value):
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None


class TVSettingsRequest(BaseModel):
    """Editable TV profile settings."""

    ip: PrivateTVIPv4
    port: int = Field(8002, ge=1, le=65535)
    client_name: str = Field("FrameArt", min_length=1, max_length=100)
    ssl: bool = True

    @field_validator("client_name")
    @classmethod
    def strip_client_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("client_name must not be empty")
        return stripped


class TVCreateRequest(TVSettingsRequest):
    """Create a persisted TV profile."""

    profile_id: ProfileId


class ManagedTVResponse(BaseModel):
    """Persisted TV profile safe for the management UI."""

    profile_id: str
    ip: str
    port: int
    client_name: str
    ssl: bool
    token_configured: bool = False


class ManagedTVsResponse(BaseModel):
    """Persisted TV profiles."""

    tvs: list[ManagedTVResponse] = Field(default_factory=list)


class ConnectionTestResponse(BaseModel):
    """Result of a provider or TV connectivity test."""

    ok: bool
    detail: str


class JobSummary(BaseModel):
    """Summary of a single job for listing."""

    job_id: str
    prompt: str | None = None
    provider: str | None = None
    content_id: str | None = None


class DeleteJobsRequest(BaseModel):
    """Request body for deleting locally stored generated jobs."""

    job_ids: list[JobId] = Field(
        ..., min_length=1, max_length=100, description="Job IDs to delete from host artifacts."
    )


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
    is_public_domain: bool = False


class PublicDomainApplyRequest(BaseModel):
    """Request body for downloading and displaying public-domain artwork."""

    source: str = Field(
        ..., min_length=1, max_length=20,
        description="Public domain source: met, aic, cma, or europeana.",
    )
    artwork_id: str = Field(..., min_length=1, max_length=300)
    tv: str | None = Field(None, max_length=100, description="TV profile name from config.")
    tv_ip: PrivateTVIPv4 | None = Field(None, description="Private TV IP address.")
    matte: str = Field("none", max_length=100, description="Matte style for upload.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings():
    """Load settings for the current request."""
    return load_settings()


def _provider_environment_key(name: str) -> str | None:
    candidates = {
        "openai": ("OPENAI_API_KEY",),
        "google": ("GOOGLE_API_KEY", "GOOGLE_AI_API_KEY"),
        "gemini": ("GOOGLE_API_KEY", "GOOGLE_AI_API_KEY"),
    }.get(name, ())
    for candidate in candidates:
        if os.environ.get(candidate):
            return candidate
    return None


def _provider_payload(config: ProviderConfig) -> dict[str, Any]:
    return config.model_dump(exclude={"api_key"}, exclude_none=True)


def _tv_payload(profile: TVProfile) -> dict[str, Any]:
    return profile.model_dump(exclude_none=True)


def _ensure_managed_providers(
    managed: dict[str, Any],
    provider_keys: dict[str, str],
    settings,
) -> dict[str, Any]:
    providers = managed.get("providers")
    if isinstance(providers, dict):
        return providers

    names = set(settings.providers)
    names.add(settings.default_provider)
    providers = {}
    for name in sorted(names):
        config = settings.providers.get(name) or ProviderConfig()
        providers[name] = _provider_payload(config)
        if config.api_key:
            provider_keys.setdefault(name, config.api_key)
    managed["providers"] = providers
    return providers


def _ensure_managed_tvs(managed: dict[str, Any], settings) -> dict[str, Any]:
    tvs = managed.get("tvs")
    if isinstance(tvs, dict):
        return tvs
    tvs = {name: _tv_payload(profile) for name, profile in settings.tvs.items()}
    managed["tvs"] = tvs
    return tvs


def _persist_management(settings, updater) -> None:
    try:
        update_management_state(settings.data_dir, updater)
    except OSError as exc:
        logger.exception("Could not persist managed settings")
        raise HTTPException(
            status_code=500,
            detail="Could not save settings. Check the FrameArt data-directory permissions.",
        ) from exc


def _managed_providers_response(settings=None) -> ManagedProvidersResponse:
    settings = settings or _settings()
    managed_keys = read_provider_secrets(settings.data_dir)
    names = set(settings.providers)
    names.add(settings.default_provider)
    providers: list[ManagedProviderResponse] = []

    for name in sorted(names):
        config = settings.providers.get(name) or ProviderConfig()
        environment_key = _provider_environment_key(name)
        if name in managed_keys:
            key_source = "managed"
        elif environment_key:
            key_source = "environment"
        elif config.api_key:
            key_source = "config"
        else:
            key_source = None
        configured_models = config.extra.get("models") if isinstance(config.extra, dict) else []
        models = [model for model in configured_models or [] if isinstance(model, str) and model]
        providers.append(
            ManagedProviderResponse(
                name=name,
                base_url=config.base_url,
                model=config.model,
                timeout=config.timeout,
                models=models,
                has_api_key=bool(config.api_key or environment_key),
                api_key_source=key_source,
                is_default=name == settings.default_provider,
            )
        )

    return ManagedProvidersResponse(
        default_provider=settings.default_provider,
        default_model=settings.default_model,
        available_types=available_providers(),
        providers=providers,
    )


def _managed_tvs_response(settings=None) -> ManagedTVsResponse:
    settings = settings or _settings()
    return ManagedTVsResponse(
        tvs=[
            ManagedTVResponse(
                profile_id=name,
                ip=profile.ip,
                port=profile.port,
                client_name=profile.name,
                ssl=profile.ssl,
                token_configured=bool(
                    profile.token_file and Path(profile.token_file).is_file()
                ),
            )
            for name, profile in sorted(settings.tvs.items())
        ]
    )


def _validate_provider_name(name: str) -> str:
    if name not in available_providers():
        available = ", ".join(available_providers())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider type. Available: {available}",
        )
    return name


def _test_provider_connection(name: str, config: ProviderConfig) -> str:
    headers: dict[str, str] = {}
    if name == "openai":
        api_key = config.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=400, detail="No OpenAI API key is configured.")
        headers["Authorization"] = f"Bearer {api_key}"
        url = (config.base_url or "https://api.openai.com/v1").rstrip("/") + "/models"
        params = None
    elif name in {"google", "gemini"}:
        api_key = (
            config.api_key
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GOOGLE_AI_API_KEY")
        )
        if not api_key:
            raise HTTPException(status_code=400, detail="No Google API key is configured.")
        base_url = config.base_url or "https://generativelanguage.googleapis.com/v1beta"
        url = base_url.rstrip("/") + "/models"
        params = {"key": api_key}
    elif name == "ollama":
        url = (config.base_url or "http://localhost:11434").rstrip("/") + "/api/tags"
        params = None
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
    else:  # guarded by _validate_provider_name
        raise HTTPException(status_code=400, detail="Unsupported provider type.")

    try:
        with httpx.Client(timeout=min(config.timeout, 15)) as client:
            response = client.get(url, headers=headers, params=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Provider returned HTTP {exc.response.status_code}.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not reach the provider endpoint.",
        ) from exc
    return f"Connected to {name}."


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


def _read_validated_upload(image: UploadFile) -> tuple[str, str, bytes]:
    """Validate upload metadata and decoded image content before writing it."""
    filename = image.filename or "upload"
    supplied_suffix = Path(filename).suffix.lower()
    mime_type = (image.content_type or "").lower()

    if supplied_suffix and supplied_suffix not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported file type. Use JPG or PNG.")
    if mime_type and mime_type not in _ALLOWED_UPLOAD_MIME:
        raise HTTPException(
            status_code=400,
            detail="Unsupported content type. Use image/jpeg or image/png.",
        )

    payload = image.file.read(_MAX_UPLOAD_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(payload) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Uploaded file is too large (max 30MB).")

    try:
        with Image.open(BytesIO(payload)) as decoded:
            image_format = (decoded.format or "").upper()
            if decoded.width * decoded.height > _MAX_UPLOAD_PIXELS:
                raise HTTPException(
                    status_code=413,
                    detail="Uploaded image is too large (max 50 megapixels).",
                )
            decoded.verify()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid image.") from exc

    if image_format not in {"JPEG", "PNG"}:
        raise HTTPException(status_code=400, detail="Unsupported image format. Use JPG or PNG.")

    actual_suffix = ".jpg" if image_format == "JPEG" else ".png"
    if supplied_suffix and (
        (supplied_suffix in {".jpg", ".jpeg"} and actual_suffix != ".jpg")
        or (supplied_suffix == ".png" and actual_suffix != ".png")
    ):
        raise HTTPException(status_code=400, detail="File extension does not match image data.")
    if mime_type and (
        (mime_type in {"image/jpeg", "image/jpg"} and actual_suffix != ".jpg")
        or (mime_type == "image/png" and actual_suffix != ".png")
    ):
        raise HTTPException(status_code=400, detail="Content type does not match image data.")

    return filename, actual_suffix, payload


def _validate_form_value(
    value: str | None,
    name: str,
    max_length: int,
    *,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail=f"{name} is required.")
        return None
    stripped = value.strip()
    if required and not stripped:
        raise HTTPException(status_code=400, detail=f"{name} cannot be empty.")
    if len(stripped) > max_length:
        raise HTTPException(
            status_code=400,
            detail=f"{name} is too long (max {max_length} characters).",
        )
    return stripped or None


def _validate_form_tv_ip(tv_ip: str | None) -> str | None:
    if not tv_ip:
        return None
    try:
        return _private_tv_ip(tv_ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _is_openai_image_model(model_id: str) -> bool:
    if not model_id:
        return False
    if model_id in {"dall-e-2", "dall-e-3", "gpt-image-1"}:
        return True
    return model_id.startswith("gpt-image-")


def _fetch_openai_image_models(openai_cfg) -> list[str]:
    """Fetch available image-capable OpenAI models for configured credentials."""
    api_key = (openai_cfg.api_key if openai_cfg else None) or os.environ.get("OPENAI_API_KEY") or ""
    if not api_key:
        return []

    base_url = (openai_cfg.base_url if openai_cfg else None) or "https://api.openai.com/v1"
    url = base_url.rstrip("/") + "/models"

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("data") if isinstance(payload, dict) else []
        models: list[str] = []
        if isinstance(items, list):
            for item in items:
                model_id = item.get("id") if isinstance(item, dict) else None
                if isinstance(model_id, str) and _is_openai_image_model(model_id):
                    models.append(model_id)
        return list(dict.fromkeys(models))
    except Exception as e:
        logger.warning("OpenAI model discovery failed: %s", e)
        return []


def _is_google_image_model_name(model_id: str) -> bool:
    """Heuristic filter for Google model IDs that can output images."""
    if not model_id:
        return False
    lowered = model_id.lower()
    image_markers = (
        "image",
        "imagen",
        "nano-banana",
    )
    return any(marker in lowered for marker in image_markers)


def _google_entry_supports_image(entry: dict[str, Any]) -> bool:
    """Detect whether a Google model entry indicates image output support."""
    for key in ("responseModalities", "outputModalities", "supportedOutputModalities"):
        vals = entry.get(key)
        if isinstance(vals, list):
            normalized = {str(v).upper() for v in vals}
            if "IMAGE" in normalized:
                return True

    methods = entry.get("supportedGenerationMethods")
    if isinstance(methods, list):
        normalized = {str(v) for v in methods}
        if any(
            m in normalized
            for m in ("generateImages", "predictImage", "generateImage", "generateContent")
        ):
            # generateContent alone is not sufficient for image output, so keep
            # this as a weak signal and fall back to name filter below.
            pass

    model_name = entry.get("name")
    if isinstance(model_name, str) and model_name.startswith("models/"):
        return _is_google_image_model_name(model_name[len("models/") :])
    return False


def _fetch_google_image_models(google_cfg) -> list[str]:
    """Fetch available Google models supporting generateContent."""
    api_key = (
        (google_cfg.api_key if google_cfg else None)
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_AI_API_KEY")
        or ""
    )
    if not api_key:
        return []

    base_url = (
        (google_cfg.base_url if google_cfg else None)
        or os.environ.get("GOOGLE_BASE_URL")
        or "https://generativelanguage.googleapis.com/v1beta"
    )
    url = base_url.rstrip("/") + "/models"

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url, params={"key": api_key})
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
                if not _google_entry_supports_image(item):
                    continue
                model_name = item.get("name")
                if isinstance(model_name, str) and model_name.startswith("models/"):
                    models.append(model_name[len("models/") :])
        return list(dict.fromkeys(models))
    except Exception as e:
        logger.warning("Google model discovery failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Routes — synchronous
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health():
    """Liveness check."""
    return HealthResponse()


@app.post("/auth/session")
def create_auth_session(req: AuthSessionRequest, response: Response):
    """Exchange an API token for an HttpOnly browser session cookie."""
    settings = _settings()
    if not settings.auth_enabled:
        return {"authenticated": True, "auth_enabled": False, "scopes": ["admin"]}

    token = req.token.get_secret_value()
    scopes = _token_scopes(settings, token)
    if not scopes:
        raise HTTPException(status_code=401, detail="Invalid API token.")

    response.set_cookie(
        key="frameart_session",
        value=token,
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"authenticated": True, "auth_enabled": True, "scopes": sorted(scopes)}


@app.get("/auth/status")
def auth_status(request: Request):
    """Return the authenticated token's effective scopes."""
    settings = _settings()
    scopes = getattr(request.state, "frameart_scopes", {"read", "control", "admin"})
    return {
        "authenticated": True,
        "auth_enabled": settings.auth_enabled,
        "scopes": sorted(scopes),
    }


@app.post("/auth/logout")
def logout(response: Response):
    """Clear the browser session cookie."""
    response.delete_cookie("frameart_session", path="/")
    return {"authenticated": False}


@app.get("/styles", response_model=dict[str, str])
def list_styles():
    """List available style presets."""
    return STYLE_PRESETS


@app.get("/providers", response_model=ProvidersResponse)
def list_providers():
    """List configured providers and models for the web UI."""
    settings = _settings()

    configured_names: set[str] = set(settings.providers.keys())
    if settings.default_provider:
        configured_names.add(settings.default_provider)

    options: list[ProviderOption] = []
    for name in sorted(configured_names):
        cfg = settings.providers.get(name)
        is_openai_family = name == "openai"
        is_google_family = name in {"google", "gemini"}
        models: list[str] = []
        if cfg and cfg.model and (
            (not is_openai_family or _is_openai_image_model(cfg.model))
            and (not is_google_family or _is_google_image_model_name(cfg.model))
        ):
            models.append(cfg.model)
        extra_models = cfg.extra.get("models") if (cfg and isinstance(cfg.extra, dict)) else None
        if isinstance(extra_models, list):
            if is_openai_family:
                models.extend(
                    [
                        m for m in extra_models
                        if isinstance(m, str) and m and _is_openai_image_model(m)
                    ]
                )
            elif is_google_family:
                models.extend(
                    [
                        m for m in extra_models
                        if isinstance(m, str) and m and _is_google_image_model_name(m)
                    ]
                )
            else:
                models.extend([m for m in extra_models if isinstance(m, str) and m])
        if (
            name == settings.default_provider
            and settings.default_model
            and (not is_openai_family or _is_openai_image_model(settings.default_model))
            and (not is_google_family or _is_google_image_model_name(settings.default_model))
        ):
            models.append(settings.default_model)
        if is_openai_family:
            models.extend(
                [
                    m for m in _fetch_openai_image_models(cfg)
                    if _is_openai_image_model(m)
                ]
            )
        if is_google_family:
            models.extend(
                [
                    m for m in _fetch_google_image_models(cfg)
                    if _is_google_image_model_name(m)
                ]
            )

        unique_models = list(dict.fromkeys(models))
        default_model = None
        if (
            name == settings.default_provider
            and settings.default_model
            and (not is_openai_family or _is_openai_image_model(settings.default_model))
            and (not is_google_family or _is_google_image_model_name(settings.default_model))
        ):
            default_model = settings.default_model
        if (
            not default_model
            and cfg
            and cfg.model
            and (not is_openai_family or _is_openai_image_model(cfg.model))
            and (not is_google_family or _is_google_image_model_name(cfg.model))
        ):
            default_model = cfg.model

        options.append(
            ProviderOption(
                name=name,
                is_default=(name == settings.default_provider),
                models=unique_models,
                default_model=default_model,
            )
        )

    return ProvidersResponse(default_provider=settings.default_provider, providers=options)


@app.get("/settings/providers", response_model=ManagedProvidersResponse)
def get_managed_providers():
    """Return editable provider settings without exposing API keys."""
    return _managed_providers_response()


@app.post("/settings/providers", response_model=ManagedProvidersResponse, status_code=201)
def create_managed_provider(req: ProviderCreateRequest):
    """Create a built-in provider configuration and optionally store its key."""
    name = _validate_provider_name(req.name)
    settings = _settings()
    existing_names = set(settings.providers) | {settings.default_provider}
    if name in existing_names:
        raise HTTPException(status_code=409, detail=f"Provider {name!r} already exists.")
    if req.clear_api_key:
        raise HTTPException(status_code=400, detail="clear_api_key is not valid when creating.")

    extra = {"models": req.models} if req.models else {}
    provider = ProviderConfig(
        base_url=req.base_url,
        model=req.model,
        timeout=req.timeout,
        extra=extra,
    )
    new_key = req.api_key.get_secret_value().strip() if req.api_key else None

    def update(managed, provider_keys):
        providers = _ensure_managed_providers(managed, provider_keys, settings)
        providers[name] = _provider_payload(provider)
        if new_key:
            provider_keys[name] = new_key

    _persist_management(settings, update)
    return _managed_providers_response(load_settings())


@app.put("/settings/providers/{name}", response_model=ManagedProvidersResponse)
def update_managed_provider(name: ProviderId, req: ProviderSettingsRequest):
    """Replace editable settings for a configured provider."""
    name = _validate_provider_name(name)
    settings = _settings()
    existing = settings.providers.get(name)
    if existing is None and name != settings.default_provider:
        raise HTTPException(status_code=404, detail=f"Provider {name!r} is not configured.")
    if req.clear_api_key and req.api_key is not None:
        raise HTTPException(status_code=400, detail="Set api_key or clear_api_key, not both.")

    existing = existing or ProviderConfig()
    extra = dict(existing.extra)
    if req.models:
        extra["models"] = req.models
    else:
        extra.pop("models", None)
    provider = ProviderConfig(
        base_url=req.base_url,
        model=req.model,
        timeout=req.timeout,
        extra=extra,
    )
    new_key = req.api_key.get_secret_value().strip() if req.api_key else None

    def update(managed, provider_keys):
        providers = _ensure_managed_providers(managed, provider_keys, settings)
        providers[name] = _provider_payload(provider)
        if req.clear_api_key:
            provider_keys.pop(name, None)
        elif new_key:
            provider_keys[name] = new_key

    _persist_management(settings, update)
    return _managed_providers_response(load_settings())


@app.delete("/settings/providers/{name}", response_model=ManagedProvidersResponse)
def delete_managed_provider(name: ProviderId):
    """Remove a provider configuration and its managed API key."""
    settings = _settings()
    if name == settings.default_provider:
        raise HTTPException(
            status_code=409,
            detail="Choose another default before deleting this provider.",
        )
    if name not in settings.providers:
        raise HTTPException(status_code=404, detail=f"Provider {name!r} is not configured.")

    def update(managed, provider_keys):
        providers = _ensure_managed_providers(managed, provider_keys, settings)
        providers.pop(name, None)
        provider_keys.pop(name, None)

    _persist_management(settings, update)
    return _managed_providers_response(load_settings())


@app.put("/settings/defaults", response_model=ManagedProvidersResponse)
def update_managed_defaults(req: DefaultsSettingsRequest):
    """Persist the default provider and optional model."""
    settings = _settings()
    configured_names = set(settings.providers) | {settings.default_provider}
    if req.provider not in configured_names:
        raise HTTPException(
            status_code=400,
            detail="The default provider must be configured first.",
        )

    def update(managed, _provider_keys):
        managed["default_provider"] = req.provider
        if req.model:
            managed["default_model"] = req.model
        else:
            managed["default_model"] = None

    _persist_management(settings, update)
    return _managed_providers_response(load_settings())


@app.post("/settings/providers/{name}/test", response_model=ConnectionTestResponse)
def test_managed_provider(name: ProviderId):
    """Test provider credentials without generating billable artwork."""
    name = _validate_provider_name(name)
    settings = _settings()
    config = settings.providers.get(name)
    if config is None and name != settings.default_provider:
        raise HTTPException(status_code=404, detail=f"Provider {name!r} is not configured.")
    detail = _test_provider_connection(name, config or ProviderConfig())
    return ConnectionTestResponse(ok=True, detail=detail)


@app.get("/settings/tvs", response_model=ManagedTVsResponse)
def get_managed_tvs():
    """Return persisted TV profiles without exposing token paths."""
    return _managed_tvs_response()


@app.post("/settings/tvs", response_model=ManagedTVsResponse, status_code=201)
def create_managed_tv(req: TVCreateRequest):
    """Create a persisted TV profile."""
    settings = _settings()
    if req.profile_id in settings.tvs:
        raise HTTPException(
            status_code=409,
            detail=f"TV profile {req.profile_id!r} already exists.",
        )
    token_file = settings.data_dir / "secrets" / f"{req.ip.replace('.', '_')}.token"
    profile = TVProfile(
        ip=req.ip,
        port=req.port,
        name=req.client_name,
        ssl=req.ssl,
        token_file=str(token_file),
    )

    def update(managed, _provider_keys):
        tvs = _ensure_managed_tvs(managed, settings)
        tvs[req.profile_id] = _tv_payload(profile)

    _persist_management(settings, update)
    return _managed_tvs_response(load_settings())


@app.put("/settings/tvs/{profile_id}", response_model=ManagedTVsResponse)
def update_managed_tv(profile_id: ProfileId, req: TVSettingsRequest):
    """Replace editable fields for a persisted TV profile."""
    settings = _settings()
    existing = settings.tvs.get(profile_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"TV profile {profile_id!r} was not found.")
    if req.ip == existing.ip and existing.token_file:
        token_file = existing.token_file
    else:
        token_file = str(settings.data_dir / "secrets" / f"{req.ip.replace('.', '_')}.token")
    profile = TVProfile(
        ip=req.ip,
        port=req.port,
        name=req.client_name,
        ssl=req.ssl,
        token_file=token_file,
    )

    def update(managed, _provider_keys):
        tvs = _ensure_managed_tvs(managed, settings)
        tvs[profile_id] = _tv_payload(profile)

    _persist_management(settings, update)
    return _managed_tvs_response(load_settings())


@app.delete("/settings/tvs/{profile_id}", response_model=ManagedTVsResponse)
def delete_managed_tv(profile_id: ProfileId):
    """Remove a persisted TV profile without deleting its recoverable pairing token."""
    settings = _settings()
    if profile_id not in settings.tvs:
        raise HTTPException(status_code=404, detail=f"TV profile {profile_id!r} was not found.")

    def update(managed, _provider_keys):
        tvs = _ensure_managed_tvs(managed, settings)
        tvs.pop(profile_id, None)

    _persist_management(settings, update)
    return _managed_tvs_response(load_settings())


@app.get("/settings/tvs/{profile_id}/test", response_model=ConnectionTestResponse)
def test_managed_tv(profile_id: ProfileId):
    """Check TV reachability and Art Mode support."""
    from frameart.tv.controller import get_status

    settings = _settings()
    profile = settings.tvs.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"TV profile {profile_id!r} was not found.")
    status = get_status(profile)
    if not status.reachable:
        return ConnectionTestResponse(ok=False, detail=status.error or "TV is not reachable.")
    if not status.art_mode_supported:
        return ConnectionTestResponse(
            ok=False,
            detail="TV is reachable but Art Mode is unavailable.",
        )
    return ConnectionTestResponse(ok=True, detail="TV is reachable and supports Art Mode.")


@app.post("/settings/tvs/{profile_id}/pair", response_model=ConnectionTestResponse)
def pair_managed_tv(profile_id: ProfileId):
    """Initiate pairing and save the TV token under the FrameArt data directory."""
    from frameart.tv.controller import _run_with_timeout, pair

    settings = _settings()
    profile = settings.tvs.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"TV profile {profile_id!r} was not found.")
    result, error = _run_with_timeout(lambda: pair(profile), timeout_sec=30)
    if error or not result:
        raise HTTPException(
            status_code=502,
            detail=f"TV pairing failed: {error or 'unknown error'}",
        )
    return ConnectionTestResponse(ok=True, detail="Pairing succeeded and the token was saved.")


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


@app.post("/upload-and-apply", response_model=JobResponse)
def upload_and_apply(
    image: UploadFile = File(..., description="Uploaded image file (jpg/png)."),  # noqa: B008
    tv: str | None = Form(None, description="TV profile name from config."),  # noqa: B008
    tv_ip: str | None = Form(None, description="TV IP address."),  # noqa: B008
    matte: str = Form("none", description="Matte style."),  # noqa: B008
    upscaler: str | None = Form(None, description="Upscaler to use."),  # noqa: B008
    no_switch: bool = Form(False, description="Upload but do not switch displayed art."),  # noqa: B008
):
    """Upload user image bytes and run import+postprocess+apply pipeline."""
    from frameart.pipeline import run_import_and_apply

    filename, suffix, payload = _read_validated_upload(image)
    tv = _validate_form_value(tv, "TV profile", 100)
    tv_ip = _validate_form_tv_ip(tv_ip)
    matte = _validate_form_value(matte, "Matte", 100, required=True) or "none"
    upscaler = _validate_form_value(upscaler, "Upscaler", 100)

    settings = _settings()
    upload_dir = settings.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / f"upload-{uuid.uuid4().hex}{suffix}"
    temp_path.write_bytes(payload)

    try:
        result = run_import_and_apply(
            settings,
            str(temp_path),
            tv_name=tv,
            tv_ip=tv_ip,
            matte=matte,
            upscaler_name=upscaler,
            no_switch=no_switch,
            source_metadata={
                "source": "web_upload",
                "filename": filename,
                "content_type": image.content_type or "",
            },
        )
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed to delete temporary upload file: %s", temp_path)

    resp = _pipeline_result_to_response(result)
    if result.error:
        raise HTTPException(status_code=500, detail=resp.model_dump())
    return resp


@app.post("/edit-and-apply", response_model=JobResponse)
def edit_and_apply(
    image: UploadFile = File(..., description="Uploaded image file (jpg/png)."),  # noqa: B008
    prompt: str = Form(..., description="Edit instruction prompt."),  # noqa: B008
    provider: str | None = Form(None, description="Provider name (e.g., openai)."),  # noqa: B008
    model: str | None = Form(None, description="Provider model ID."),  # noqa: B008
    upscaler: str | None = Form(None, description="Upscaler to use."),  # noqa: B008
    tv: str | None = Form(None, description="TV profile name from config."),  # noqa: B008
    tv_ip: str | None = Form(None, description="TV IP address."),  # noqa: B008
    matte: str = Form("none", description="Matte style."),  # noqa: B008
    no_upload: bool = Form(False, description="Edit and process only; skip TV upload."),  # noqa: B008
    no_switch: bool = Form(False, description="Upload but do not switch displayed art."),  # noqa: B008
):
    """Upload + edit an image with prompt, then post-process and apply to TV."""
    from frameart.pipeline import run_edit_and_apply

    edit_prompt = _validate_form_value(prompt, "Edit prompt", 4000, required=True)
    if edit_prompt is None:  # pragma: no cover - required=True guarantees this
        raise HTTPException(status_code=400, detail="Edit prompt cannot be empty.")
    filename, suffix, payload = _read_validated_upload(image)
    provider = _validate_form_value(provider, "Provider", 100)
    model = _validate_form_value(model, "Model", 200)
    upscaler = _validate_form_value(upscaler, "Upscaler", 100)
    tv = _validate_form_value(tv, "TV profile", 100)
    tv_ip = _validate_form_tv_ip(tv_ip)
    matte = _validate_form_value(matte, "Matte", 100, required=True) or "none"

    settings = _settings()
    upload_dir = settings.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / f"edit-upload-{uuid.uuid4().hex}{suffix}"
    temp_path.write_bytes(payload)

    try:
        result = run_edit_and_apply(
            settings,
            str(temp_path),
            edit_prompt,
            provider_name=provider,
            model=model,
            upscaler_name=upscaler,
            tv_name=tv,
            tv_ip=tv_ip,
            matte=matte,
            no_upload=no_upload,
            no_switch=no_switch,
        )
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed to delete temporary upload file: %s", temp_path)

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
    from frameart.tv.controller import get_status

    profile = _resolve_tv_profile(tv, tv_ip)

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
    from frameart.tv.discovery import SSDPDiscoveryError, discover

    try:
        tvs = discover(timeout=timeout, frame_only=frame_only)
    except SSDPDiscoveryError as exc:
        logger.warning("TV discovery unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return [
        DiscoveredTVResponse(ip=tv.ip, name=tv.name, model=tv.model, frame_tv=tv.frame_tv)
        for tv in tvs
    ]


# ---------------------------------------------------------------------------
# Routes — TV art management
# ---------------------------------------------------------------------------


def _resolve_tv_profile(tv: str | None, tv_ip: str | None):
    """Resolve a TVProfile from query params / request body, or raise 400."""
    settings = _settings()
    profile = None
    if tv:
        profile = settings.tvs.get(tv)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Unknown TV profile {tv!r}.")
    elif tv_ip:
        try:
            profile = TVProfile(ip=tv_ip)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    try:
        artworks = list_art_deduplicated(profile)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"TV art list failed: {e}",
        ) from e
    return [
        TVArtItem(
            content_id=a.get("content_id", "unknown"),
            is_favourite=a.get("is_favourite", False),
        )
        for a in artworks
    ]


@app.get("/tv/art/thumbnail")
def tv_art_thumbnail(
    content_id: str = Query(
        ..., min_length=1, max_length=128, pattern=_IDENTIFIER_RE,
        description="Artwork content ID.",
    ),
    tv: str | None = Query(None, max_length=100, description="TV profile name."),
    tv_ip: str | None = Query(None, max_length=45, description="Private TV IP address."),
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
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Could not verify favourites; deletion was cancelled. "
                    "Retry or set include_favorites=true explicitly."
                ),
            ) from exc

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


@app.post("/tv/art/display")
def tv_display_art(req: DisplayArtRequest):
    """Switch the TV display to an existing artwork by content ID."""
    from frameart.tv.controller import switch_art

    profile = _resolve_tv_profile(req.tv, req.tv_ip)
    if switch_art(profile, req.content_id):
        return {"ok": True, "content_id": req.content_id}
    raise HTTPException(status_code=500, detail="Failed to display artwork.")


@app.get("/tv/mattes")
def tv_mattes(
    tv: str | None = Query(None, description="TV profile name from config."),
    tv_ip: str | None = Query(None, description="TV IP address."),
):
    """List matte styles supported by the TV."""
    from frameart.tv.controller import get_matte_list

    profile = _resolve_tv_profile(tv, tv_ip)
    try:
        return get_matte_list(profile)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TV matte list failed: {e}") from e


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
    source: str = Query(
        ..., min_length=1, max_length=20,
        description="Catalog source: met, aic, cma, or europeana.",
    ),
    q: str = Query(..., min_length=1, max_length=500, description="Search query."),
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
            artwork = PublicDomainArtwork(**item)
            if not artwork.is_public_domain:
                dropped += 1
                continue
            validated.append(artwork)
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


def _validated_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID.")
    return job_id


def _find_job_dirs(artifacts_dir: Path, job_id: str) -> list[Path]:
    """Resolve a job ID without interpolating it into a glob expression."""
    safe_job_id = _validated_job_id(job_id)
    if not artifacts_dir.exists():
        return []
    return [
        candidate
        for candidate in artifacts_dir.glob("*/*/*/*")
        if candidate.is_dir() and candidate.name == safe_job_id
    ]


@app.get("/jobs", response_model=list[JobSummary])
def list_jobs(limit: int = Query(20, ge=1, le=200, description="Max jobs to return.")):
    """List recent generated jobs."""
    import json as _json

    settings = _settings()
    artifacts_dir = settings.data_dir / "artifacts"
    if not artifacts_dir.exists():
        return []

    meta_files = sorted(artifacts_dir.rglob("meta.json"), reverse=True)
    jobs: list[JobSummary] = []
    for meta_path in meta_files:
        job_dir = meta_path.parent
        # Only show jobs that have an image preview the UI can render.
        if not (job_dir / "final.png").exists() and not (job_dir / "source.png").exists():
            continue
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
        if len(jobs) >= limit:
            break
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
        job_dirs = [
            job_dir
            for job_dir in _find_job_dirs(artifacts_dir, job_id)
            if (job_dir / "meta.json").is_file()
        ]
        if not job_dirs:
            not_found.append(job_id)
            continue

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

    for job_dir in _find_job_dirs(artifacts_dir, job_id):
        final_path = job_dir / "final.png"
        if final_path.is_file():
            return FileResponse(final_path, media_type="image/png")
        source_path = job_dir / "source.png"
        if source_path.is_file():
            return FileResponse(source_path, media_type="image/png")

    raise HTTPException(status_code=404, detail=f"No image found for job {job_id}")


class JobApplyRequest(BaseModel):
    """Request body for uploading a previously generated job's image to a TV."""

    tv: str | None = Field(None, max_length=100, description="TV profile name from config.")
    tv_ip: PrivateTVIPv4 | None = Field(None, description="Private TV IP address.")
    matte: str = Field(
        "none", max_length=100,
        description="Matte style (e.g., none, shadowbox_polar, shadowbox_noir).",
    )


class EditFromExistingRequest(BaseModel):
    """Request body for creating a new image from existing artwork."""

    prompt: Prompt = Field(..., description="Edit/generation instruction prompt.")
    provider: str | None = Field(None, max_length=100, description="Provider name.")
    model: str | None = Field(None, max_length=200, description="Provider model ID.")
    upscaler: str | None = Field(None, max_length=100, description="Upscaler to use.")
    tv: str | None = Field(None, max_length=100, description="Target TV profile name.")
    tv_ip: PrivateTVIPv4 | None = Field(None, description="Private target TV IP address.")
    matte: str = Field("none", max_length=100, description="Matte style.")
    no_upload: bool = Field(False, description="Edit/process only; skip TV upload.")
    no_switch: bool = Field(False, description="Upload but do not switch displayed art.")


class TVArtEditRequest(EditFromExistingRequest):
    """Request body for creating a new image from art already stored on a TV."""

    content_id: ContentId = Field(..., description="Source TV artwork content ID.")
    source_tv: str | None = Field(
        None, max_length=100, description="Source TV profile name from config."
    )
    source_tv_ip: PrivateTVIPv4 | None = Field(None, description="Private source TV IP.")


def _find_job_image_path(settings, job_id: str) -> Path:
    """Find a generated job image path (prefer final, then source) or raise 404."""
    artifacts_dir = settings.data_dir / "artifacts"
    for job_dir in _find_job_dirs(artifacts_dir, job_id):
        final_path = job_dir / "final.png"
        if final_path.is_file():
            return final_path
        source_path = job_dir / "source.png"
        if source_path.is_file():
            return source_path

    raise HTTPException(status_code=404, detail=f"No image found for job {job_id}")


def _find_artifact_image_by_content_id(
    settings,
    content_id: str,
    source_tv_ip: str | None = None,
) -> Path | None:
    """Find the newest local artifact image that maps to a TV ``content_id``."""
    import json as _json

    artifacts_dir = settings.data_dir / "artifacts"
    if not artifacts_dir.exists():
        return None

    for meta_path in sorted(artifacts_dir.rglob("meta.json"), reverse=True):
        try:
            meta = _json.loads(meta_path.read_text())
        except Exception:
            continue

        matched = False
        if meta.get("content_id") == content_id:
            matched = True

        tv_map = meta.get("tv_content_ids")
        if (
            not matched
            and isinstance(tv_map, dict)
            and (
                (source_tv_ip and tv_map.get(source_tv_ip) == content_id)
                or any(str(v) == content_id for v in tv_map.values())
            )
        ):
            matched = True

        if not matched:
            continue

        job_dir = meta_path.parent
        final_path = job_dir / "final.png"
        if final_path.exists():
            return final_path
        source_path = job_dir / "source.png"
        if source_path.exists():
            return source_path

    return None


@app.post("/jobs/{job_id}/edit-and-apply", response_model=JobResponse)
def edit_job_artwork(job_id: str, req: EditFromExistingRequest):
    """Create a new image by editing an existing server-side artwork job image."""
    from frameart.pipeline import run_edit_and_apply

    settings = _settings()
    selected_image = _find_job_image_path(settings, job_id)
    edit_prompt = req.prompt.strip()
    if not edit_prompt:
        raise HTTPException(status_code=400, detail="Edit prompt cannot be empty.")

    result = run_edit_and_apply(
        settings,
        str(selected_image),
        edit_prompt,
        provider_name=req.provider,
        model=req.model,
        upscaler_name=req.upscaler,
        tv_name=req.tv,
        tv_ip=req.tv_ip,
        matte=req.matte,
        no_upload=req.no_upload,
        no_switch=req.no_switch,
    )
    resp = _pipeline_result_to_response(result)
    if result.error:
        raise HTTPException(status_code=500, detail=resp.model_dump())
    return resp


@app.post("/tv/art/edit-and-apply", response_model=JobResponse)
def edit_tv_artwork(req: TVArtEditRequest):
    """Create a new image by editing artwork currently stored on a TV."""
    from frameart.pipeline import run_edit_and_apply
    from frameart.tv.controller import get_art_thumbnail

    settings = _settings()
    edit_prompt = req.prompt.strip()
    if not edit_prompt:
        raise HTTPException(status_code=400, detail="Edit prompt cannot be empty.")

    source_profile = None
    try:
        source_profile = _resolve_tv_profile(req.source_tv, req.source_tv_ip)
    except HTTPException:
        source_profile = None

    source_image = _find_artifact_image_by_content_id(
        settings,
        req.content_id,
        source_tv_ip=(source_profile.ip if source_profile else req.source_tv_ip),
    )
    source_temp_path: Path | None = None
    if source_image is None:
        if source_profile is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No source TV specified and no local artifact match found. "
                    "Provide source_tv/source_tv_ip."
                ),
            )

        source_bytes = get_art_thumbnail(source_profile, req.content_id)
        if not source_bytes:
            raise HTTPException(status_code=404, detail="TV artwork thumbnail not available.")

        upload_dir = settings.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        source_temp_path = upload_dir / f"tv-art-edit-{uuid.uuid4().hex}.jpg"
        source_temp_path.write_bytes(source_bytes)
        source_image = source_temp_path

    target_tv = req.tv
    target_tv_ip = req.tv_ip
    if not req.no_upload and not (target_tv or target_tv_ip):
        # Default target is the same TV as source when available.
        target_tv = req.source_tv
        if source_profile is not None:
            target_tv_ip = source_profile.ip

    try:
        result = run_edit_and_apply(
            settings,
            str(source_image),
            edit_prompt,
            provider_name=req.provider,
            model=req.model,
            upscaler_name=req.upscaler,
            tv_name=target_tv,
            tv_ip=target_tv_ip,
            matte=req.matte,
            no_upload=req.no_upload,
            no_switch=req.no_switch,
        )
    finally:
        if source_temp_path is not None:
            try:
                source_temp_path.unlink(missing_ok=True)
            except Exception:
                logger.warning(
                    "Failed to delete temporary TV edit source file: %s",
                    source_temp_path,
                )

    resp = _pipeline_result_to_response(result)
    if result.error:
        raise HTTPException(status_code=500, detail=resp.model_dump())
    return resp


@app.post("/jobs/{job_id}/apply", response_model=JobResponse)
def apply_job_to_tv(job_id: str, req: JobApplyRequest):
    """Upload a previously generated job's image to a TV."""
    import json as _json

    from frameart.pipeline import run_apply
    from frameart.tv.controller import list_art_deduplicated, switch_art

    settings = _settings()
    selected_image = _find_job_image_path(settings, job_id)

    # Reuse existing TV content when possible to avoid duplicate uploads.
    job_dir = selected_image.parent
    meta_path = job_dir / "meta.json"
    meta: dict[str, Any] = {}
    existing_content_id: str | None = None
    tv_content_ids: dict[str, str] = {}
    try:
        if meta_path.exists():
            meta = _json.loads(meta_path.read_text())
            tv_map = meta.get("tv_content_ids")
            if isinstance(tv_map, dict):
                for key, value in tv_map.items():
                    if isinstance(key, str) and isinstance(value, str) and value:
                        tv_content_ids[key] = value
            content_id = meta.get("content_id")
            if isinstance(content_id, str) and content_id:
                existing_content_id = content_id
    except Exception:
        existing_content_id = None

    profile = None
    try:
        profile = _resolve_tv_profile(req.tv, req.tv_ip)
    except HTTPException:
        profile = None

    candidate_ids: list[str] = []
    if profile:
        mapped_id = tv_content_ids.get(profile.ip)
        if mapped_id:
            candidate_ids.append(mapped_id)
    if existing_content_id and existing_content_id not in candidate_ids:
        candidate_ids.append(existing_content_id)

    if profile and candidate_ids:
        try:
            tv_art = list_art_deduplicated(profile)
            tv_ids = {str(a.get("content_id", "")) for a in tv_art}
        except Exception:
            tv_ids = set()

        reusable_content_id = next((cid for cid in candidate_ids if cid in tv_ids), None)
        if reusable_content_id and switch_art(profile, reusable_content_id):
            source_preview = job_dir / "source.png"
            final_preview = job_dir / "final.png"
            return JobResponse(
                job_id=job_id,
                job_dir=str(job_dir),
                source_path=str(source_preview) if source_preview.exists() else None,
                final_path=str(final_preview) if final_preview.exists() else None,
                content_id=reusable_content_id,
                tv_switched=True,
                metadata={
                    "job_id": job_id,
                    "content_id": reusable_content_id,
                    "tv_ip": profile.ip,
                    "tv_switched": True,
                    "reused_existing_content": True,
                },
                timings={},
                error=None,
            )

    result = run_apply(
        settings,
        str(selected_image),
        tv_name=req.tv,
        tv_ip=req.tv_ip,
        matte=req.matte,
    )
    resp = _pipeline_result_to_response(result)
    if result.error:
        raise HTTPException(status_code=500, detail=resp.model_dump())

    # Persist applied content ID back to the original job metadata for dedupe on re-apply.
    try:
        persisted_meta = meta if isinstance(meta, dict) else {}
        if result.content_id:
            persisted_meta["content_id"] = result.content_id
            result_tv_ip = (
                result.metadata.get("tv_ip")
                if isinstance(result.metadata, dict)
                else None
            )
            target_tv_ip = (
                result_tv_ip
                if isinstance(result_tv_ip, str)
                else (profile.ip if profile else req.tv_ip)
            )
            if isinstance(target_tv_ip, str) and target_tv_ip:
                tv_map = persisted_meta.get("tv_content_ids")
                if not isinstance(tv_map, dict):
                    tv_map = {}
                tv_map[target_tv_ip] = result.content_id
                persisted_meta["tv_content_ids"] = tv_map
        meta_path.write_text(_json.dumps(persisted_meta, indent=2, default=str))
    except Exception:
        logger.warning("Failed to persist re-apply metadata for job_id=%s", job_id)

    return resp


# ---------------------------------------------------------------------------
# Routes — async job queue
# ---------------------------------------------------------------------------


@app.get("/jobs/{job_id}/status", response_model=AsyncJobDetail)
def get_job_status(job_id: str):
    """Get the detailed status of an async job."""
    from frameart.jobs import job_store

    _validated_job_id(job_id)
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


@app.get("/async/jobs", response_model=list[AsyncJobDetail])
def list_async_jobs(
    limit: int = Query(50, ge=1, le=200, description="Maximum number of async jobs to return."),
):
    """List recent async jobs (newest first)."""
    from frameart.jobs import job_store

    jobs = job_store.list_jobs(limit=limit)
    out: list[AsyncJobDetail] = []
    for job in jobs:
        result_response = None
        if job.result is not None:
            result_response = _pipeline_result_to_response(job.result)
        out.append(
            AsyncJobDetail(
                job_id=job.id,
                status=job.status.value,
                request=job.request,
                result=result_response,
                error=job.error,
            )
        )
    return out


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
        request_summary={
            "type": "generate",
            "prompt": req.prompt,
            "style": req.style,
            "provider": req.provider,
            "model": req.model,
        },
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
            "provider": req.provider,
            "model": req.model,
            "tv": req.tv,
            "tv_ip": req.tv_ip,
        },
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


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _ensure_admin_token(settings) -> tuple[Path | None, bool]:
    """Load or create the persistent bootstrap token for authenticated serving."""
    configured = _secret_value(settings.admin_token)
    if configured:
        return None, False

    secrets_dir = settings.data_dir / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path = secrets_dir / "admin-api.token"
    created = False
    if token_path.exists():
        token = token_path.read_text().strip()
        if len(token) < 20:
            raise RuntimeError(f"Admin token file is invalid: {token_path}")
    else:
        token = secrets.token_urlsafe(32)
        token_path.write_text(token)
        token_path.chmod(0o600)
        created = True

    # Environment values outrank YAML and are visible to the single uvicorn worker.
    os.environ["FRAMEART_ADMIN_TOKEN"] = token
    return token_path, created


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the uvicorn server."""
    import uvicorn

    settings = load_settings()
    if not _is_loopback_host(host) and not settings.auth_enabled:
        raise RuntimeError(
            "Refusing a non-loopback bind without API authentication. Set "
            "FRAMEART_AUTH_ENABLED=true or bind to 127.0.0.1."
        )

    if settings.auth_enabled:
        token_path, created = _ensure_admin_token(settings)
        if token_path:
            state = "Created" if created else "Using"
            print(f"{state} FrameArt admin token file: {token_path}")
            if created:
                print(f"Bootstrap admin token: {token_path.read_text().strip()}")

    # Background jobs and per-TV locks are process-local, so multiple workers
    # would cause intermittent job 404s and unsafe concurrent device operations.
    uvicorn.run("frameart.api:app", host=host, port=port, workers=1)
