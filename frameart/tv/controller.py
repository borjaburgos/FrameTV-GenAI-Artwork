"""Samsung Frame TV controller — upload art, switch display, manage pairing.

Uses the ``samsungtvws`` library (xchwarze/samsung-tv-ws-api).
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import random
import socket as _socket
import ssl
import time
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image
from samsungtvws import SamsungTVWS
from samsungtvws import exceptions as _tv_exc
from samsungtvws.art import ArtChannelEmitCommand
from samsungtvws.event import D2D_SERVICE_MESSAGE_EVENT
from samsungtvws.helper import process_api_response

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


def _connect(profile: TVProfile) -> SamsungTVWS:
    """Create a SamsungTVWS connection from a TVProfile.

    Always provides a token_file so that the samsungtvws library can persist
    the pairing token received from the TV. Without a token_file, tokens are
    only kept in-memory and lost between connections — which causes write
    operations (like send_image) to fail with error -1.
    """
    token_file = profile.token_file

    # Auto-discover saved token or create a path for a new one
    if not token_file:
        token_file = _find_token_file(profile.ip)
    if not token_file:
        token_file = _token_path_for_ip(profile.ip)

    _ensure_token_dir(token_file)

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
        art = tv.art()
        return art.get_artmode()

    def _query_current():
        art = tv.art()
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


_UPLOAD_CHUNK = 64 * 1024  # 64 KB chunks for TCP socket transfer
_ART_EVENT_TIMEOUT = 30  # seconds to wait for a TV art-app response


def _art_upload(
    art,
    file_bytes: bytes,
    file_type: str = "jpg",
    matte: str = "shadowbox_polar",
) -> str:
    """Upload an image to a Samsung Frame TV and return the content_id.

    Detects the TV's art-API version and picks the right transport:

    * **API 0.97** (2018/2019 Frame TVs) — the image is packed into a
      single WebSocket *binary* frame together with a compact JSON
      header.  These TVs do **not** support the D2D socket handshake
      and return ``error -1`` if you try.
    * **Newer APIs** (2020 +) — the traditional D2D socket flow is
      used: a ``send_image`` text request, then a TCP socket transfer.

    The API-0.97 path was reverse-engineered from SmartThings traffic
    and upstreamed in ``xchwarze/samsung-tv-ws-api#181``.
    """
    # Ensure the WebSocket is open before anything else
    if not art.connection:
        art.open()

    # ---- Detect API version ----
    api_version: str | None = None
    try:
        api_version = art.get_api_version()
        logger.info("TV art API version: %s", api_version)
    except Exception as exc:
        logger.debug("Could not query API version (%s), assuming modern API", exc)

    if api_version == "0.97":
        return _upload_ws_binary(art, file_bytes, file_type, matte)
    return _upload_d2d_socket(art, file_bytes, file_type, matte)


# ---------------------------------------------------------------------------
# API 0.97 — WebSocket binary frame upload (2018/2019 Frame TVs)
# ---------------------------------------------------------------------------

def _upload_ws_binary(
    art,
    file_bytes: bytes,
    file_type: str,
    matte: str,
) -> str:
    """Upload via a single WebSocket binary frame (API 0.97).

    Wire format:
        [uint16-BE header_len] [JSON header (utf-8)] [raw image bytes]

    The JSON header is an ``ms.channel.emit`` envelope wrapping the
    ``send_image`` request.  SmartThings sends ``"JPEG"`` (uppercase)
    for the ``file_type`` field.
    """
    upload_id = str(uuid.uuid4())

    # SmartThings sends uppercase file types for API 0.97
    ft = file_type.lower()
    ft_hdr = "JPEG" if ft in ("jpg", "jpeg") else ft.upper()

    inner: dict[str, Any] = {
        "request": "send_image",
        "file_type": ft_hdr,
        "matte_id": matte or "none",
        "id": upload_id,
    }
    outer: dict[str, Any] = {
        "method": "ms.channel.emit",
        "params": {
            "data": json.dumps(inner),
            "to": "host",
            "event": "art_app_request",
        },
    }

    header = json.dumps(outer, separators=(",", ":")).encode("utf-8")
    if len(header) > 0xFFFF:
        raise ValueError("Upload header too large for uint16 length prefix")

    payload = len(header).to_bytes(2, "big") + header + file_bytes

    assert art.connection
    logger.info(
        "Uploading via WS binary (API 0.97): header=%d bytes, image=%d bytes",
        len(header), len(file_bytes),
    )
    art.connection.send_binary(payload)

    # Wait for the TV to confirm
    result = _wait_for_art_event(art, "image_added", upload_id, "send_image")
    content_id: str = result["content_id"]
    logger.info("Upload complete (API 0.97), content_id=%s", content_id)
    return content_id


# ---------------------------------------------------------------------------
# Modern API — D2D socket upload (2020+ Frame TVs)
# ---------------------------------------------------------------------------

def _upload_d2d_socket(
    art,
    file_bytes: bytes,
    file_type: str,
    matte: str,
) -> str:
    """Upload via a D2D TCP socket handshake (modern API).

    Includes ``request_id`` in the ``send_image`` request, which is
    required by many firmware versions (NickWaterton fork fix).
    """
    file_size = len(file_bytes)
    file_type = file_type.lower()
    if file_type == "jpeg":
        file_type = "jpg"

    request_uuid = str(uuid.uuid4())
    date = datetime.now().strftime("%Y:%m:%d %H:%M:%S")

    request_data: dict[str, Any] = {
        "request": "send_image",
        "file_type": file_type,
        "request_id": request_uuid,
        "id": request_uuid,
        "conn_info": {
            "d2d_mode": "socket",
            "connection_id": random.randrange(4 * 1024 * 1024 * 1024),
            "id": request_uuid,
        },
        "image_date": date,
        "matte_id": matte,
        "file_size": file_size,
    }

    logger.debug("Sending send_image request (D2D): %s", json.dumps(request_data))
    art.send_command(ArtChannelEmitCommand.art_app_request(request_data))

    # ---- Wait for "ready_to_use" d2d response ----
    conn_info = _wait_for_art_event(
        art, "ready_to_use", request_uuid, "send_image",
    )
    d2d_info = json.loads(conn_info["conn_info"])

    # ---- Open TCP socket and send image data ----
    art_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        art_sock.connect((d2d_info["ip"], int(d2d_info["port"])))
        if d2d_info.get("secured", False):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            art_sock = ctx.wrap_socket(art_sock)

        header = json.dumps({
            "num": 0,
            "total": 1,
            "fileLength": file_size,
            "fileName": "frameart_upload",
            "fileType": file_type,
            "secKey": d2d_info["key"],
            "version": "0.0.1",
        })
        art_sock.sendall(len(header).to_bytes(4, "big"))
        art_sock.sendall(header.encode("ascii"))
        for pos in range(0, file_size, _UPLOAD_CHUNK):
            art_sock.sendall(file_bytes[pos : pos + _UPLOAD_CHUNK])
    finally:
        art_sock.close()

    logger.debug("Image data sent, waiting for image_added confirmation")

    # ---- Wait for "image_added" confirmation ----
    result = _wait_for_art_event(art, "image_added", request_uuid, "send_image")
    content_id: str = result["content_id"]
    return content_id


def _wait_for_art_event(
    art,
    expected_sub_event: str,
    request_uuid: str,
    request_name: str,
    timeout: int = _ART_EVENT_TIMEOUT,
) -> dict[str, Any]:
    """Read WebSocket messages until we get the expected d2d sub-event.

    Matches responses by ``request_id`` or ``id`` so stray events
    (like ``clientDisconnect``) are silently skipped.

    Raises ``TimeoutError`` if no matching response arrives within
    *timeout* seconds.
    """
    import websocket as _ws_mod

    assert art.connection
    deadline = time.monotonic() + timeout
    # Set socket-level timeout so recv() doesn't block forever
    if hasattr(art.connection, "sock") and art.connection.sock:
        art.connection.sock.settimeout(timeout)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Timed out after {timeout}s waiting for '{expected_sub_event}' "
                f"response to `{request_name}`"
            )
        try:
            raw = art.connection.recv()
        except _ws_mod.WebSocketTimeoutException:
            raise TimeoutError(
                f"Timed out after {timeout}s waiting for '{expected_sub_event}' "
                f"response to `{request_name}`"
            ) from None

        response = process_api_response(raw)
        event = response.get("event", "*")
        art._websocket_event(event, response)

        if event != D2D_SERVICE_MESSAGE_EVENT:
            logger.debug("Skipping non-d2d event: %s", event)
            continue

        data = json.loads(response["data"])
        # Match by request_id or id to ignore responses for stale requests
        resp_id = data.get("request_id", data.get("id"))
        if resp_id != request_uuid:
            logger.debug(
                "Skipping d2d event for different request: %s (want %s)",
                resp_id, request_uuid,
            )
            continue

        sub_event = data.get("event", "*")
        if sub_event == "error":
            raise _tv_exc.ResponseError(
                f"`{request_name}` request failed "
                f"with error number {data.get('error_code', '?')}"
            )
        if sub_event == expected_sub_event:
            return data

        logger.debug("Skipping unexpected sub-event: %s (want %s)", sub_event, expected_sub_event)


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
    # Samsung TVs prefer JPEG and reject large PNGs
    upload_bytes, upload_type = _prepare_image_for_tv(image_bytes, file_type)

    # Normalize file type for the TV API ("jpeg" -> "jpg" internally)
    ft = upload_type.lower()
    if ft == "jpeg":
        ft = "jpg"

    # Resolve matte: use library default when the user hasn't picked one
    effective_matte = matte if matte and matte != "none" else "shadowbox_polar"

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

    tv: SamsungTVWS | None = None

    def _do_upload() -> str:
        nonlocal tv
        # Close any leftover connection from a previous attempt so we
        # don't pile up stale WebSocket clients on the TV.
        if tv is not None:
            with contextlib.suppress(Exception):
                tv.close()

        tv = _connect(profile)
        art = tv.art()

        logger.debug(
            "Upload details: host=%s port=%d size=%d bytes file_type=%s "
            "matte=%s token_file=%s",
            profile.ip, profile.port, len(upload_bytes), ft,
            effective_matte, tv.token_file,
        )

        # Use our custom upload that includes `request_id` in the
        # send_image request — required by many Samsung firmware versions.
        # See: xchwarze/samsung-tv-ws-api#130
        content_id = _art_upload(
            art, upload_bytes, file_type=ft, matte=effective_matte,
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
        if tv is not None:
            with contextlib.suppress(Exception):
                tv.close()


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


