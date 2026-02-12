"""Samsung Frame TV controller — upload art, switch display, manage pairing.

Uses the ``samsungtvws`` library (xchwarze/samsung-tv-ws-api).
"""

from __future__ import annotations

import logging
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from samsungtvws import SamsungTVWS

from frameart.config import TVProfile

# Samsung TVs use self-signed certs — suppress urllib3 SSL warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [2, 4, 8]
DEFAULT_TIMEOUT = 10  # seconds for websocket operations


@dataclass
class TVStatus:
    """Current status of a Samsung Frame TV."""

    reachable: bool
    art_mode_supported: bool = False
    art_mode_on: bool = False
    current_artwork: str | None = None
    error: str | None = None


@dataclass
class UploadResult:
    """Result of uploading an image to the TV."""

    content_id: str
    success: bool
    error: str | None = None


def _ensure_token_dir(token_file: str) -> None:
    """Create the parent directory for the token file if it doesn't exist."""
    parent = Path(token_file).parent
    parent.mkdir(parents=True, exist_ok=True)


def _find_token_file(ip: str) -> str | None:
    """Look for an existing token file for the given IP."""
    from frameart.config import _default_data_dir

    secrets_dir = _default_data_dir() / "secrets"
    candidate = secrets_dir / f"{ip.replace('.', '_')}.token"
    if candidate.is_file():
        return str(candidate)
    return None


def _connect(profile: TVProfile) -> SamsungTVWS:
    """Create a SamsungTVWS connection from a TVProfile."""
    token_file = profile.token_file

    # Auto-discover saved token if none specified
    if not token_file:
        token_file = _find_token_file(profile.ip)

    if token_file:
        _ensure_token_dir(token_file)

    return SamsungTVWS(
        host=profile.ip,
        port=profile.port,
        token_file=token_file,
        name=profile.name,
        timeout=DEFAULT_TIMEOUT,
    )


def _retry(func, description: str) -> Any:
    """Execute a function with retry and exponential backoff."""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return func()
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %ds",
                    description, attempt + 1, MAX_RETRIES, e, wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "%s failed after %d attempts: %s",
                    description, MAX_RETRIES, e,
                )
    raise RuntimeError(f"{description} failed after {MAX_RETRIES} attempts: {last_error}")


def pair(profile: TVProfile) -> bool:
    """Initiate pairing with the TV.

    The TV will display an "Allow" prompt. The user must accept it on the TV.
    After acceptance, the token is saved to the token_file.

    Returns True if connection succeeds.
    """
    if not profile.token_file:
        from frameart.config import _default_data_dir

        secrets_dir = _default_data_dir() / "secrets"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        profile.token_file = str(
            secrets_dir / f"{profile.ip.replace('.', '_')}.token"
        )

    logger.info(
        "Pairing with TV at %s:%d (token will be saved to %s)",
        profile.ip, profile.port, profile.token_file,
    )

    tv = _connect(profile)
    try:
        # open() triggers the pairing prompt on the TV
        tv.open()
        # rest_device_info() confirms connectivity via REST (no websocket)
        info = tv.rest_device_info()
        logger.info("Connected to TV: %s", info.get("device", {}).get("name", info))
        tv.close()
        return True
    except Exception as e:
        logger.error("Pairing failed: %s", e)
        raise


def get_status(profile: TVProfile) -> TVStatus:
    """Check the current status of the Frame TV."""
    try:
        tv = _connect(profile)
        tv.rest_device_info()  # REST check — confirms TV is reachable
    except Exception as e:
        return TVStatus(reachable=False, error=str(e))

    try:
        art = tv.art()
    except Exception as e:
        return TVStatus(reachable=True, error=f"Could not access art interface: {e}")

    try:
        supported = art.supported()
    except Exception:
        supported = False

    if not supported:
        return TVStatus(reachable=True, art_mode_supported=False)

    art_mode_on = False
    current_artwork = None

    try:
        art_mode_on = art.get_artmode()
    except Exception as e:
        logger.warning("Could not get art mode status: %s", e)

    try:
        current = art.get_current()
        if isinstance(current, dict):
            current_artwork = current.get("content_id")
        elif isinstance(current, str):
            current_artwork = current
    except Exception as e:
        logger.warning("Could not get current artwork: %s", e)

    return TVStatus(
        reachable=True,
        art_mode_supported=True,
        art_mode_on=art_mode_on,
        current_artwork=current_artwork,
    )


def upload_image(
    profile: TVProfile,
    image_bytes: bytes,
    file_type: str = "PNG",
    matte: str = "none",
) -> UploadResult:
    """Upload an image to the TV's art collection.

    Parameters
    ----------
    profile:
        TV connection profile.
    image_bytes:
        Raw image data.
    file_type:
        'PNG' or 'JPEG'.
    matte:
        Matte style (e.g., 'modern_black', 'none').

    Returns
    -------
    UploadResult with the content_id assigned by the TV.
    """

    def _do_upload() -> str:
        tv = _connect(profile)
        art = tv.art()

        # samsungtvws expects lowercase file_type: "png", "jpeg"
        ft = file_type.lower()
        if ft == "jpg":
            ft = "jpeg"

        kwargs: dict[str, Any] = {"file_type": ft}
        if matte and matte != "none":
            kwargs["matte"] = matte

        content_id = art.upload(image_bytes, **kwargs)
        return content_id

    try:
        content_id = _retry(_do_upload, "Upload image")
        logger.info("Uploaded image, content_id=%s", content_id)
        return UploadResult(content_id=content_id, success=True)
    except Exception as e:
        return UploadResult(content_id="", success=False, error=str(e))


def switch_art(profile: TVProfile, content_id: str) -> bool:
    """Switch the displayed artwork on the Frame TV.

    Also attempts to put the TV into Art Mode if it isn't already.
    """

    def _do_switch() -> None:
        tv = _connect(profile)
        art = tv.art()

        # Try to enter art mode first
        try:
            art.set_artmode(True)
        except Exception as e:
            logger.warning("Could not set art mode (may already be on): %s", e)

        art.select_image(content_id)

    try:
        _retry(_do_switch, f"Switch art to {content_id}")
        logger.info("Switched display to content_id=%s", content_id)
        return True
    except Exception as e:
        logger.error("Failed to switch art: %s", e)
        return False


def list_art(profile: TVProfile) -> list[dict[str, Any]]:
    """List all artworks available on the TV."""
    tv = _connect(profile)
    art = tv.art()
    return art.available()


def delete_art(profile: TVProfile, content_id: str) -> bool:
    """Delete an artwork from the TV by content_id."""
    try:
        tv = _connect(profile)
        art = tv.art()
        art.delete(content_id)
        logger.info("Deleted content_id=%s", content_id)
        return True
    except Exception as e:
        logger.error("Failed to delete %s: %s", content_id, e)
        return False
