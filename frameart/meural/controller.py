"""Netgear Meural canvas controller — local REST API.

All communication uses the unauthenticated local HTTP API at
``http://{ip}/remote/``.  No cloud account or tokens are required.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from PIL import Image

from frameart.config import MeuralProfile

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [2, 4, 8]
DEFAULT_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MeuralStatus:
    """Current status of a Meural canvas."""

    reachable: bool
    sleeping: bool = False
    orientation: str | None = None
    current_gallery: str | None = None
    current_item: str | None = None
    device_name: str | None = None
    device_model: str | None = None
    error: str | None = None


@dataclass
class DisplayResult:
    """Result of sending an image to the Meural for display."""

    success: bool
    error: str | None = None


@dataclass
class GalleryInfo:
    """Summary of a gallery on the Meural."""

    id: str
    name: str
    item_count: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _base_url(profile: MeuralProfile) -> str:
    return f"http://{profile.ip}:{profile.port}/remote"


def _get(profile: MeuralProfile, path: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Issue a GET request to the Meural local API and return the JSON body."""
    url = f"{_base_url(profile)}/{path.lstrip('/')}"
    logger.debug("GET %s", url)
    resp = httpx.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _post_multipart(
    profile: MeuralProfile,
    path: str,
    *,
    field_name: str,
    data: bytes,
    content_type: str,
    timeout: float = 30,
) -> dict[str, Any]:
    """POST multipart/form-data to the Meural local API."""
    url = f"{_base_url(profile)}/{path.lstrip('/')}"
    logger.debug("POST %s (%d bytes, %s)", url, len(data), content_type)
    files = {field_name: ("image", data, content_type)}
    resp = httpx.post(url, files=files, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _retry(func, description: str) -> Any:
    """Execute *func* with retry and exponential backoff."""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return func()
        except Exception as e:
            last_error = e
            logger.debug(
                "%s error (attempt %d): %s: %s",
                description, attempt + 1, type(e).__name__, e,
            )
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %ds",
                    description, attempt + 1, MAX_RETRIES, e, wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "%s failed after %d attempts: %s", description, MAX_RETRIES, e,
                )
    raise RuntimeError(f"{description} failed after {MAX_RETRIES} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------

# Meural Gen2 native resolution: 1920×1080 (landscape) / 1080×1920 (portrait)
# Meural Gen3 (Canvas II): 1920×1080 or 3840×2160 depending on model
MEURAL_LANDSCAPE = (1920, 1080)
MEURAL_PORTRAIT = (1080, 1920)


def _prepare_image(image_bytes: bytes, orientation: str) -> tuple[bytes, str]:
    """Convert image to JPEG suitable for the Meural.

    The Meural accepts image/jpeg and image/png via the postcard endpoint.
    JPEG is smaller and avoids any alpha-channel issues.

    Returns (jpeg_bytes, content_type).
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    jpeg_bytes = buf.getvalue()

    logger.debug(
        "Prepared image for Meural: %dx%d orientation=%s size=%.1f KB",
        img.width, img.height, orientation, len(jpeg_bytes) / 1024,
    )
    return jpeg_bytes, "image/jpeg"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def identify(profile: MeuralProfile) -> dict[str, Any]:
    """Identify the Meural device (model, firmware, etc.)."""
    return _get(profile, "identify/")


def get_status(profile: MeuralProfile) -> MeuralStatus:
    """Check the current status of the Meural canvas."""
    try:
        info = identify(profile)
    except Exception as e:
        return MeuralStatus(reachable=False, error=str(e))

    # Sleep check
    sleeping = False
    try:
        sleep_resp = _get(profile, "control_check/sleep/")
        sleeping = sleep_resp.get("response", "") == "suspended"
    except Exception:
        pass

    # Gallery status
    current_gallery = None
    current_item = None
    try:
        gallery = _get(profile, "get_gallery_status_json/")
        resp = gallery.get("response", {})
        current_gallery = str(resp.get("current_gallery", ""))
        current_item = str(resp.get("current_item", ""))
    except Exception:
        pass

    return MeuralStatus(
        reachable=True,
        sleeping=sleeping,
        orientation=info.get("response", {}).get("orientation"),
        current_gallery=current_gallery,
        current_item=current_item,
        device_name=info.get("response", {}).get("alias", ""),
        device_model=info.get("response", {}).get("model", ""),
    )


def display_image(
    profile: MeuralProfile,
    image_bytes: bytes,
    *,
    duration: int = 0,
) -> DisplayResult:
    """Upload and display an image on the Meural via the postcard endpoint.

    Parameters
    ----------
    profile:
        Meural connection profile.
    image_bytes:
        Raw image data (PNG or JPEG).
    duration:
        How long (in seconds) the image should be displayed before the
        Meural returns to its normal playlist.  ``0`` means the image
        stays displayed indefinitely (slideshow paused).

    Returns
    -------
    DisplayResult indicating success or failure.
    """
    orientation = profile.orientation or "vertical"
    jpeg_bytes, content_type = _prepare_image(image_bytes, orientation)

    def _do_upload() -> dict[str, Any]:
        return _post_multipart(
            profile,
            "postcard/",
            field_name="photo",
            data=jpeg_bytes,
            content_type=content_type,
        )

    try:
        result = _retry(_do_upload, "Meural postcard upload")
        status = result.get("status", "")
        if status != "pass":
            return DisplayResult(
                success=False,
                error=f"Meural returned status={status}: {result.get('response', '')}",
            )
        logger.info("Image displayed on Meural at %s", profile.ip)

        # If duration == 0, pause the slideshow so the image stays up.
        # If duration > 0, we leave the slideshow running — the Meural
        # will auto-advance after its configured previewDuration.
        if duration == 0:
            try:
                _pause_slideshow(profile)
            except Exception as exc:
                logger.warning("Could not pause slideshow: %s", exc)

        return DisplayResult(success=True)
    except Exception as e:
        return DisplayResult(success=False, error=str(e))


def _pause_slideshow(profile: MeuralProfile) -> None:
    """Pause slideshow by navigating to the current item (keeps it on screen)."""
    # The local API has no direct pause command.  However, re-selecting
    # the current item effectively keeps it displayed.
    try:
        gallery = _get(profile, "get_gallery_status_json/")
        current = gallery.get("response", {}).get("current_item")
        if current:
            _get(profile, f"control_command/change_item/{current}")
            logger.debug("Re-selected current item %s to hold display", current)
    except Exception as exc:
        logger.debug("Pause workaround failed: %s", exc)


# --- Orientation ---

def set_orientation(profile: MeuralProfile, orientation: str) -> bool:
    """Set the canvas orientation ('portrait' or 'landscape')."""
    if orientation not in ("portrait", "landscape"):
        raise ValueError(f"Invalid orientation: {orientation!r}")

    try:
        _get(profile, f"control_command/set_orientation/{orientation}")
        logger.info("Meural orientation set to %s", orientation)
        return True
    except Exception as e:
        logger.error("Failed to set orientation: %s", e)
        return False


# --- Brightness ---

def set_brightness(profile: MeuralProfile, level: int) -> bool:
    """Set backlight brightness (0-100)."""
    level = max(0, min(100, level))
    try:
        _get(profile, f"control_command/set_backlight/{level}/")
        logger.info("Meural brightness set to %d", level)
        return True
    except Exception as e:
        logger.error("Failed to set brightness: %s", e)
        return False


def reset_brightness(profile: MeuralProfile) -> bool:
    """Reset brightness to auto (ambient light sensor)."""
    try:
        _get(profile, "control_command/als_calibrate/off/")
        logger.info("Meural brightness reset to auto")
        return True
    except Exception as e:
        logger.error("Failed to reset brightness: %s", e)
        return False


# --- Sleep / Wake ---

def sleep(profile: MeuralProfile) -> bool:
    """Put the canvas to sleep (screen off)."""
    try:
        _get(profile, "control_command/suspend")
        logger.info("Meural put to sleep")
        return True
    except Exception as e:
        logger.error("Failed to sleep: %s", e)
        return False


def wake(profile: MeuralProfile) -> bool:
    """Wake the canvas (screen on)."""
    try:
        _get(profile, "control_command/resume")
        logger.info("Meural woken up")
        return True
    except Exception as e:
        logger.error("Failed to wake: %s", e)
        return False


# --- Navigation ---

def next_image(profile: MeuralProfile) -> bool:
    """Navigate to the next image in the current playlist."""
    try:
        _get(profile, "control_command/set_key/right/")
        return True
    except Exception as e:
        logger.error("Failed to navigate next: %s", e)
        return False


def previous_image(profile: MeuralProfile) -> bool:
    """Navigate to the previous image in the current playlist."""
    try:
        _get(profile, "control_command/set_key/left/")
        return True
    except Exception as e:
        logger.error("Failed to navigate previous: %s", e)
        return False


def toggle_info_card(profile: MeuralProfile) -> bool:
    """Toggle the information card overlay."""
    try:
        _get(profile, "control_command/set_key/up/")
        return True
    except Exception as e:
        logger.error("Failed to toggle info card: %s", e)
        return False


# --- Gallery ---

def list_galleries(profile: MeuralProfile) -> list[GalleryInfo]:
    """List all galleries on the Meural."""
    resp = _get(profile, "get_galleries_json/")
    galleries: list[GalleryInfo] = []
    for g in resp.get("response", []):
        galleries.append(GalleryInfo(
            id=str(g.get("id", "")),
            name=str(g.get("name", "")),
            item_count=int(g.get("item_count", 0)),
        ))
    return galleries


def change_gallery(profile: MeuralProfile, gallery_id: str) -> bool:
    """Switch to a specific gallery/playlist."""
    try:
        _get(profile, f"control_command/change_gallery/{gallery_id}")
        logger.info("Switched to gallery %s", gallery_id)
        return True
    except Exception as e:
        logger.error("Failed to change gallery: %s", e)
        return False


def change_item(profile: MeuralProfile, item_id: str) -> bool:
    """Display a specific artwork by item ID."""
    try:
        _get(profile, f"control_command/change_item/{item_id}")
        logger.info("Switched to item %s", item_id)
        return True
    except Exception as e:
        logger.error("Failed to change item: %s", e)
        return False


def list_gallery_items(
    profile: MeuralProfile, gallery_id: str,
) -> list[dict[str, Any]]:
    """List items in a specific gallery."""
    resp = _get(profile, f"get_frame_items_by_gallery_json/{gallery_id}")
    return resp.get("response", [])
