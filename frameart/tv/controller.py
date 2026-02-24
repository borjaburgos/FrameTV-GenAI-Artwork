"""Samsung Frame TV controller — upload art, switch display, manage pairing.

Uses the ``samsungtvws`` library v3.x (xchwarze/samsung-tv-ws-api).
"""

from __future__ import annotations

import contextlib
import io
import logging
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image
from samsungtvws import SamsungTVArt, SamsungTVWS

from frameart.config import TVProfile

# Samsung TVs use self-signed certs — suppress urllib3 SSL warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [2, 4, 8]
DEFAULT_TIMEOUT = 10  # seconds for websocket operations

# Samsung Frame TVs reject large uploads over WebSocket.
# Convert images to JPEG to keep size reasonable.
_JPEG_QUALITY = 95
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB safety threshold


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


def _token_path_for_ip(ip: str) -> str:
    """Return the canonical token file path for a TV IP address."""
    from frameart.config import _default_data_dir

    secrets_dir = _default_data_dir() / "secrets"
    return str(secrets_dir / f"{ip.replace('.', '_')}.token")


def _resolve_token_file(profile: TVProfile) -> str:
    """Return the token file path for a profile, auto-discovering or creating as needed."""
    token_file = profile.token_file
    if not token_file:
        token_file = _find_token_file(profile.ip)
    if not token_file:
        token_file = _token_path_for_ip(profile.ip)
    _ensure_token_dir(token_file)
    return token_file


def _connect(profile: TVProfile) -> SamsungTVWS:
    """Create a SamsungTVWS connection from a TVProfile.

    Used for REST-only operations (pairing, device info).  For art operations,
    use ``_connect_art()`` instead.
    """
    token_file = _resolve_token_file(profile)

    logger.debug(
        "Connecting to %s:%d token_file=%s (exists=%s)",
        profile.ip, profile.port, token_file, Path(token_file).is_file(),
    )

    return SamsungTVWS(
        host=profile.ip,
        port=profile.port,
        token_file=token_file,
        name=profile.name,
        timeout=DEFAULT_TIMEOUT,
    )


def _connect_art(profile: TVProfile) -> SamsungTVArt:
    """Create a SamsungTVArt connection from a TVProfile.

    In samsungtvws v3.x, ``SamsungTVArt`` is a standalone class that handles
    API version detection, upload transports (WS binary for API 0.97, D2D
    socket for modern APIs), SSL, and request correlation internally.
    """
    token_file = _resolve_token_file(profile)

    logger.debug(
        "Connecting art to %s:%d token_file=%s (exists=%s)",
        profile.ip, profile.port, token_file, Path(token_file).is_file(),
    )

    return SamsungTVArt(
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
            logger.debug(
                "%s error detail (attempt %d): %s: %s",
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


def _run_with_timeout(func, timeout_sec: int = 8):
    """Run a function in a thread with a timeout. Returns (result, error)."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(func)
        try:
            return future.result(timeout=timeout_sec), None
        except concurrent.futures.TimeoutError:
            return None, "timed out"
        except Exception as e:
            return None, str(e)


def get_status(profile: TVProfile) -> TVStatus:
    """Check the current status of the Frame TV."""
    # Step 1: REST-only reachability check (no websocket)
    try:
        tv = _connect(profile)
        device_info = tv.rest_device_info()
    except Exception as e:
        return TVStatus(reachable=False, error=str(e))

    # Step 2: Check FrameTVSupport from REST response (no websocket)
    device = device_info.get("device", {})
    is_support_str = device_info.get("isSupport", "{}")
    frame_supported = (
        device.get("FrameTVSupport") == "true"
        or '"FrameTVSupport":"true"' in is_support_str
    )

    if not frame_supported:
        return TVStatus(reachable=True, art_mode_supported=False)

    # Step 3: Try art websocket calls with a thread timeout
    # These can hang if the TV doesn't respond, so we cap them.
    art_mode_on = False
    current_artwork = None

    def _query_art_mode():
        art = _connect_art(profile)
        return art.get_artmode()

    def _query_current():
        art = _connect_art(profile)
        return art.get_current()

    result, err = _run_with_timeout(_query_art_mode)
    if err:
        logger.warning("Could not get art mode status: %s", err)
    else:
        art_mode_on = bool(result)

    result, err = _run_with_timeout(_query_current)
    if err:
        logger.warning("Could not get current artwork: %s", err)
    else:
        if isinstance(result, dict):
            current_artwork = result.get("content_id")
        elif isinstance(result, str):
            current_artwork = result

    return TVStatus(
        reachable=True,
        art_mode_supported=True,
        art_mode_on=art_mode_on,
        current_artwork=current_artwork,
    )


# --- Image preparation -------------------------------------------------------


def _prepare_image_for_tv(
    image_bytes: bytes, file_type: str,
) -> tuple[bytes, str]:
    """Convert image to JPEG for TV upload if needed.

    Samsung Frame TVs reject large uploads via WebSocket. PNG at 3840x2160
    can be 15-25 MB; JPEG at the same size is 1-3 MB.

    Returns (image_bytes, file_type) ready for the TV.
    """
    size_mb = len(image_bytes) / (1024 * 1024)
    logger.debug(
        "Preparing image for TV: input_format=%s input_size=%.2f MB",
        file_type, size_mb,
    )

    if file_type.upper() in ("PNG",) or len(image_bytes) > _MAX_UPLOAD_BYTES:
        logger.info(
            "Converting %s (%.1f MB) to JPEG for TV upload",
            file_type, size_mb,
        )
        img = Image.open(io.BytesIO(image_bytes))
        logger.debug("Image dimensions: %dx%d mode=%s", img.width, img.height, img.mode)
        img = img.convert("RGB")  # drop alpha if present
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
        jpeg_bytes = buf.getvalue()
        new_mb = len(jpeg_bytes) / (1024 * 1024)
        logger.info("Converted to JPEG: %.1f MB -> %.1f MB", size_mb, new_mb)
        return jpeg_bytes, "JPEG"

    logger.debug("No conversion needed (format=%s, size=%.2f MB)", file_type, size_mb)
    return image_bytes, file_type


# --- Upload -------------------------------------------------------------------


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
        Matte style (e.g., 'shadowbox_polar', 'none').

    Returns
    -------
    UploadResult with the content_id assigned by the TV.
    """
    # Samsung TVs prefer JPEG and reject large PNGs
    upload_bytes, upload_type = _prepare_image_for_tv(image_bytes, file_type)

    # Normalize file type for the TV API
    ft = upload_type.lower()
    if ft == "jpeg":
        ft = "jpg"

    # Pass the matte through as-is; callers are responsible for providing
    # a valid matte_id (use ``get_matte_list`` to discover supported values).
    effective_matte = matte or "shadowbox_polar"

    # Validate image bytes before attempting upload
    if len(upload_bytes) < 100:
        return UploadResult(
            content_id="", success=False,
            error=f"Image too small ({len(upload_bytes)} bytes) — likely corrupt",
        )
    if ft == "jpg" and upload_bytes[:2] != b"\xff\xd8":
        logger.warning("Expected JPEG but magic bytes are %r", upload_bytes[:4])

    logger.info(
        "Uploading %s image (%.1f KB, file_type=%s, matte=%s)",
        upload_type, len(upload_bytes) / 1024, ft, effective_matte,
    )

    art: SamsungTVArt | None = None

    def _do_upload() -> str:
        nonlocal art
        # Close any leftover connection from a previous attempt so we
        # don't pile up stale WebSocket clients on the TV.
        if art is not None:
            with contextlib.suppress(Exception):
                art.close()

        art = _connect_art(profile)

        logger.debug(
            "Upload details: host=%s port=%d size=%d bytes file_type=%s "
            "matte=%s token_file=%s",
            profile.ip, profile.port, len(upload_bytes), ft,
            effective_matte, art.token_file,
        )

        # samsungtvws v3.x handles API version detection, WS binary (0.97),
        # D2D socket (modern), SSL, request_id, and sendall internally.
        content_id = art.upload(
            file=upload_bytes, matte=effective_matte, file_type=ft,
        )
        logger.debug("TV returned content_id=%s", content_id)
        return content_id

    try:
        content_id = _retry(_do_upload, "Upload image")
        logger.info("Uploaded image, content_id=%s", content_id)
        return UploadResult(content_id=content_id, success=True)
    except Exception as e:
        error_msg = str(e)
        if isinstance(e, TimeoutError):
            error_msg += (
                "\n\nHint: The TV did not respond in time. This can happen "
                "when the TV's art service is in a bad state. "
                "Try power-cycling the TV, then retry."
            )
        elif "error number -1" in error_msg:
            error_msg += (
                "\n\nHint: The TV rejected the upload (error -1). "
                "Common causes:\n"
                "  - 2019 Frame TVs need a power cycle after repeated failures\n"
                "  - Try re-pairing: frameart tv pair --tv-ip "
                f"{profile.ip}\n"
                "  - Ensure the TV screen is on (not in standby)"
            )
        return UploadResult(content_id="", success=False, error=error_msg)
    finally:
        if art is not None:
            with contextlib.suppress(Exception):
                art.close()


# --- Art management -----------------------------------------------------------


def switch_art(profile: TVProfile, content_id: str) -> bool:
    """Switch the displayed artwork on the Frame TV.

    Also attempts to put the TV into Art Mode if it isn't already.
    """

    def _do_switch() -> None:
        art = _connect_art(profile)

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
    art = _connect_art(profile)
    return art.available()


def get_matte_list(profile: TVProfile) -> list[dict[str, Any]]:
    """Query the TV for its supported matte types.

    Returns a list of dicts, each with at least a ``matte_id`` key.
    The samsungtvws v3.x library handles both ``matte_type_list`` (modern)
    and ``matte_list`` (API 0.97) response keys internally.
    """
    art = _connect_art(profile)
    result = art.get_matte_list()
    # v3.x returns {"matte_types": [...], "matte_colors": [...]}
    if isinstance(result, dict):
        return result.get("matte_types", [])
    # Fallback for unexpected return types
    return result


def change_matte(profile: TVProfile, content_id: str, matte_id: str) -> bool:
    """Change the matte/frame on an already-uploaded artwork.

    Parameters
    ----------
    profile:
        TV connection profile.
    content_id:
        The content ID of the artwork (e.g., ``MY_F0006``).
    matte_id:
        The matte ID to apply (use ``get_matte_list`` to see valid values).

    Returns
    -------
    True on success, False on failure.
    """
    art = _connect_art(profile)
    try:
        art.change_matte(content_id, matte_id)
        logger.info("Changed matte on %s to %s", content_id, matte_id)
        return True
    except Exception as e:
        logger.error("Failed to change matte: %s", e)
        return False
