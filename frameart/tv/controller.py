"""Samsung Frame TV controller — upload art, switch display, manage pairing.

Uses the ``samsungtvws`` library v3.x (xchwarze/samsung-tv-ws-api).
"""

from __future__ import annotations

import contextlib
import io
import logging
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
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


class TVOperationError(RuntimeError):
    """Base error for bounded TV operation failures."""


class TVOperationBusyError(TVOperationError):
    """Raised when an operation expires before it can start."""


class TVOperationTimeoutError(TVOperationError):
    """Raised when an active TV operation exceeds its response deadline."""


class _TVOperationGate:
    """A cancellable per-TV gate that prioritizes mutations over reads."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_lease: int | None = None
        self._next_lease = 1
        self._quarantined_reads: set[int] = set()
        self._waiting_mutations = 0

    def acquire(self, timeout_sec: float, priority: str) -> int | None:
        deadline = time.monotonic() + max(0.0, timeout_sec)
        mutation = priority == "mutation"
        acquired = False
        with self._condition:
            if mutation:
                self._waiting_mutations += 1
            try:
                # A read that exceeded its deadline may still have a blocked
                # library thread. Refuse more reads until that thread exits so
                # repeated refreshes cannot create an unbounded pile of them.
                if not mutation and self._quarantined_reads:
                    return None

                while self._active_lease is not None or (
                    not mutation and self._waiting_mutations > 0
                ):
                    if not mutation and self._quarantined_reads:
                        return None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)

                if not mutation and self._quarantined_reads:
                    return None

                lease = self._next_lease
                self._next_lease += 1
                self._active_lease = lease
                acquired = True
                return lease
            finally:
                if mutation:
                    self._waiting_mutations -= 1
                if not acquired:
                    self._condition.notify_all()

    def release(self, lease: int) -> None:
        with self._condition:
            if lease in self._quarantined_reads:
                self._quarantined_reads.remove(lease)
            elif self._active_lease == lease:
                self._active_lease = None
            self._condition.notify_all()

    def quarantine_read(self, lease: int) -> None:
        """Release a timed-out read without letting late cleanup release its successor."""
        with self._condition:
            if self._active_lease == lease:
                self._active_lease = None
                self._quarantined_reads.add(lease)
                self._condition.notify_all()


_TV_OP_GATES: dict[str, _TVOperationGate] = {}
_TV_OP_GATES_GUARD = threading.Lock()


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


def _retry(
    func,
    description: str,
    *,
    cancel_event: threading.Event | None = None,
) -> Any:
    """Execute a function with retry and exponential backoff."""
    last_error = None
    for attempt in range(MAX_RETRIES):
        if cancel_event is not None and cancel_event.is_set():
            raise TVOperationTimeoutError(f"{description}: cancelled after timeout")
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
                if cancel_event is not None:
                    if cancel_event.wait(wait):
                        raise TVOperationTimeoutError(
                            f"{description}: cancelled after timeout"
                        ) from e
                else:
                    time.sleep(wait)
            else:
                logger.error(
                    "%s failed after %d attempts: %s",
                    description, MAX_RETRIES, e,
                )
    raise RuntimeError(f"{description} failed after {MAX_RETRIES} attempts: {last_error}")


def _tv_operation_gate(profile: TVProfile) -> _TVOperationGate:
    """Return the cancellable, priority-aware gate for one TV."""
    key = f"{profile.ip}:{profile.port}"
    with _TV_OP_GATES_GUARD:
        gate = _TV_OP_GATES.get(key)
        if gate is None:
            gate = _TVOperationGate()
            _TV_OP_GATES[key] = gate
        return gate


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


TV_OP_TIMEOUT = 20  # seconds — cap for any single TV WebSocket operation
ART_READINESS_TIMEOUT = 3.0
ART_READINESS_POLL_INTERVAL = 0.25


def _run_with_timeout(func, timeout_sec: float = TV_OP_TIMEOUT):
    """Run a function in a thread with a timeout. Returns (result, error)."""
    import queue

    outcomes: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def _invoke() -> None:
        try:
            outcomes.put((True, func()))
        except Exception as exc:
            outcomes.put((False, exc))

    # Python cannot safely kill a blocked thread. A daemon thread gives callers
    # a real response deadline without making process shutdown wait forever.
    worker = threading.Thread(target=_invoke, daemon=True, name="frameart-tv-op")
    worker.start()
    worker.join(timeout_sec)
    if worker.is_alive():
        return None, "timed out"

    succeeded, value = outcomes.get_nowait()
    if succeeded:
        return value, None
    return None, str(value)


def _run_tv_op(
    profile: TVProfile,
    func,
    description: str,
    timeout_sec: float = TV_OP_TIMEOUT,
    *,
    priority: str = "mutation",
    cancel=None,
):
    """Run a TV operation through a cancellable, priority-aware device gate.

    Waiting happens in the caller thread. An expired waiter therefore never
    creates a background worker and can never contact the TV later. Once an
    operation starts, the gate stays occupied until its worker actually exits,
    even if the caller's response deadline expires.
    """
    if priority not in {"read", "mutation"}:
        raise ValueError("priority must be 'read' or 'mutation'")

    started_at = time.monotonic()
    gate = _tv_operation_gate(profile)
    lease = gate.acquire(timeout_sec, priority)
    if lease is None:
        raise TVOperationBusyError(
            f"{description}: TV busy; operation expired before it could start"
        )

    remaining = timeout_sec - (time.monotonic() - started_at)
    if remaining <= 0:
        gate.release(lease)
        raise TVOperationBusyError(
            f"{description}: TV busy; operation expired before it could start"
        )

    cancel_event = threading.Event()

    def _inner():
        try:
            return _retry(func, description, cancel_event=cancel_event)
        finally:
            gate.release(lease)

    result, err = _run_with_timeout(_inner, timeout_sec=remaining)
    if err:
        if err == "timed out":
            cancel_event.set()
            if priority == "read":
                # A stale read must not permanently block uploads or display
                # changes. Its lease is quarantined so late cleanup cannot
                # release a newer mutation's lease.
                gate.quarantine_read(lease)
            if cancel is not None:
                with contextlib.suppress(Exception):
                    cancel()
            raise TVOperationTimeoutError(f"{description}: timed out")
        raise TVOperationError(f"{description}: {err}")
    return result


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

    try:
        result = _run_art_call(
            profile,
            lambda art: art.get_artmode(),
            "Get art mode status",
            priority="read",
        )
        art_mode_on = _art_mode_is_on(result)
    except TVOperationError as exc:
        logger.warning("Could not get art mode status: %s", exc)

    try:
        result = _run_art_call(
            profile,
            lambda art: art.get_current(),
            "Get current artwork",
            priority="read",
        )
        if isinstance(result, dict):
            current_artwork = result.get("content_id")
        elif isinstance(result, str):
            current_artwork = result
    except TVOperationError as exc:
        logger.warning("Could not get current artwork: %s", exc)

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
        img = ImageOps.exif_transpose(img)
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
    effective_matte = matte or "none"

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

    def _do_upload(art: SamsungTVArt) -> str:
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
        content_id = _run_art_call(
            profile,
            _do_upload,
            "Upload image",
            timeout_sec=60,
        )
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


# --- Art management -----------------------------------------------------------


def _run_art_call(
    profile: TVProfile,
    func,
    description: str,
    *,
    timeout_sec: float = TV_OP_TIMEOUT,
    priority: str = "mutation",
):
    """Run one art-service call with its own connection and deadline."""

    state_lock = threading.Lock()
    cancelled = threading.Event()
    active_art: list[SamsungTVArt | None] = [None]
    active_sockets: set[Any] = set()

    def _abort_art(art: SamsungTVArt | None) -> None:
        connection = getattr(art, "connection", None)
        abort = getattr(connection, "abort", None)
        if callable(abort):
            with contextlib.suppress(Exception):
                abort()

    def _cancel() -> None:
        cancelled.set()
        with state_lock:
            art = active_art[0]
            sockets = list(active_sockets)
        _abort_art(art)
        for sock in sockets:
            with contextlib.suppress(Exception):
                sock.close()

    def _call():
        art = None
        try:
            if cancelled.is_set():
                raise TVOperationTimeoutError(f"{description}: cancelled after timeout")
            art = _connect_art(profile)

            # samsungtvws may move thumbnail bytes over a secondary D2D
            # socket. Track it as well as the WebSocket so cancellation can
            # wake either blocking receive path.
            open_d2d = getattr(art, "_open_d2d_socket", None)
            if callable(open_d2d):
                def _open_tracked_socket(*args, **kwargs):
                    sock = open_d2d(*args, **kwargs)
                    with state_lock:
                        if cancelled.is_set():
                            with contextlib.suppress(Exception):
                                sock.close()
                            raise TVOperationTimeoutError(
                                f"{description}: cancelled after timeout"
                            )
                        active_sockets.add(sock)
                    return sock

                art._open_d2d_socket = _open_tracked_socket

            with state_lock:
                active_art[0] = art
                should_abort = cancelled.is_set()
            if should_abort:
                _abort_art(art)
                raise TVOperationTimeoutError(
                    f"{description}: cancelled after timeout"
                )
            return func(art)
        finally:
            with state_lock:
                if active_art[0] is art:
                    active_art[0] = None
                active_sockets.clear()
            if art is not None:
                with contextlib.suppress(Exception):
                    art.close()

    return _run_tv_op(
        profile,
        _call,
        description,
        timeout_sec=timeout_sec,
        priority=priority,
        cancel=_cancel,
    )


def _art_mode_is_on(value: Any) -> bool:
    """Normalize the bool/string states returned by different TV generations."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "on", "true", "yes"}
    return bool(value)


def _content_id_from_current(value: Any) -> str | None:
    if isinstance(value, dict):
        content_id = value.get("content_id")
        return str(content_id) if content_id else None
    if isinstance(value, str):
        return value
    return None


def _get_current_artwork(
    profile: TVProfile,
    *,
    timeout_sec: float = TV_OP_TIMEOUT,
) -> str | None:
    current = _run_art_call(
        profile,
        lambda art: art.get_current(),
        "Get current artwork",
        timeout_sec=timeout_sec,
    )
    return _content_id_from_current(current)


def wait_for_art(
    profile: TVProfile,
    content_id: str,
    *,
    timeout_sec: float = ART_READINESS_TIMEOUT,
) -> bool:
    """Briefly wait for a newly uploaded content ID to appear in the TV library.

    Readiness is advisory: callers should still attempt selection if the TV's
    content-list operation is unavailable or exceeds this bounded deadline.
    """
    if timeout_sec <= 0:
        return True

    def _wait(art) -> bool:
        deadline = time.monotonic() + timeout_sec
        while True:
            artworks = art.available()
            if any(item.get("content_id") == content_id for item in artworks):
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(ART_READINESS_POLL_INTERVAL, remaining))

    try:
        ready = bool(
            _run_art_call(
                profile,
                _wait,
                f"Wait for artwork {content_id}",
                timeout_sec=timeout_sec,
            )
        )
    except Exception as exc:
        logger.warning("Could not confirm artwork readiness for %s: %s", content_id, exc)
        return False

    if not ready:
        logger.warning("Artwork %s was not listed before the readiness deadline", content_id)
    return ready


def switch_art(
    profile: TVProfile,
    content_id: str,
    *,
    wait_for_ready: bool = False,
    readiness_timeout_sec: float = ART_READINESS_TIMEOUT,
) -> bool:
    """Switch the displayed artwork on the Frame TV.

    Art-mode detection, enabling Art Mode, and image selection each receive an
    independent bounded deadline. If selection fails or times out, query the TV
    once more and accept success when the requested content is already current.
    """
    if wait_for_ready:
        wait_for_art(profile, content_id, timeout_sec=readiness_timeout_sec)

    art_mode_on = False
    try:
        art_mode_on = _art_mode_is_on(
            _run_art_call(profile, lambda art: art.get_artmode(), "Get art mode status")
        )
    except Exception as exc:
        logger.warning("Could not get art mode status before switching: %s", exc)

    if not art_mode_on:
        try:
            _run_art_call(profile, lambda art: art.set_artmode(True), "Enable art mode")
        except Exception as exc:
            # Some TVs report an error even when Art Mode is already active.
            # Selection still gets its own connection and deadline below.
            logger.warning("Could not enable art mode before switching: %s", exc)

    try:
        _run_art_call(
            profile,
            lambda art: art.select_image(content_id),
            f"Switch art to {content_id}",
        )
        logger.info("Switched display to content_id=%s", content_id)
        return True
    except Exception as exc:
        logger.warning("Artwork selection did not complete for %s: %s", content_id, exc)

    try:
        current = _get_current_artwork(profile)
    except Exception as exc:
        logger.error("Failed to reconcile display state for %s: %s", content_id, exc)
        return False

    if current == content_id:
        logger.info(
            "Selection response failed, but the TV confirms content_id=%s is displayed",
            content_id,
        )
        return True

    logger.error(
        "Failed to switch art to %s; TV reports current artwork %s",
        content_id,
        current or "unknown",
    )
    return False


def list_art(profile: TVProfile) -> list[dict[str, Any]]:
    """List all artworks available on the TV (raw, includes duplicates across categories)."""
    return _run_art_call(
        profile,
        lambda art: art.available(),
        "List art",
        priority="read",
    )


def list_art_deduplicated(profile: TVProfile) -> list[dict[str, Any]]:
    """List artworks on the TV, deduplicated with ``is_favourite`` annotated.

    The TV returns each artwork once per category:
    MY-C0002 = user uploads, MY-C0003 = all, MY-C0004 = favourites.
    This function deduplicates by ``content_id`` and adds a boolean
    ``is_favourite`` key based on MY-C0004 membership.
    """
    raw = list_art(profile)

    fav_ids: set[str] = set()
    for item in raw:
        if item.get("category_id") == "MY-C0004":
            fav_ids.add(item.get("content_id", ""))

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in raw:
        cid = item.get("content_id", "")
        if cid and cid not in seen:
            seen.add(cid)
            unique.append({**item, "is_favourite": cid in fav_ids})

    return unique


def get_art_thumbnail(profile: TVProfile, content_id: str) -> bytes | None:
    """Fetch thumbnail bytes for a TV artwork content ID.

    Returns ``None`` if the TV does not provide a thumbnail for the content.
    """
    def _do_thumbnail(art: SamsungTVArt) -> bytes | None:
        data = art.get_thumbnail(content_id)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        return None

    return _run_art_call(
        profile,
        _do_thumbnail,
        f"Fetch thumbnail for {content_id}",
        priority="read",
    )


def get_art_thumbnails(
    profile: TVProfile,
    content_ids: list[str],
) -> dict[str, bytes]:
    """Fetch several thumbnails with the TV's single-request batch transport.

    Older Frame models support ``get_thumbnail_list`` even when repeated
    ``get_thumbnail`` calls stall. Besides being a compatibility path, this
    keeps a cold gallery to one bounded TV operation instead of one operation
    per artwork.
    """
    requested = list(dict.fromkeys(content_ids))
    if not requested:
        return {}

    requested_set = set(requested)

    def _do_thumbnails(art: SamsungTVArt) -> dict[str, bytes]:
        raw = art.get_thumbnail_list(requested)
        if not isinstance(raw, dict):
            return {}

        thumbnails: dict[str, bytes] = {}
        for filename, data in raw.items():
            if not isinstance(data, (bytes, bytearray)):
                continue
            raw_name = str(filename)
            content_id = raw_name if raw_name in requested_set else Path(raw_name).stem
            if content_id in requested_set:
                thumbnails[content_id] = bytes(data)
        return thumbnails

    return _run_art_call(
        profile,
        _do_thumbnails,
        f"Fetch {len(requested)} thumbnails",
        priority="read",
    )


def get_matte_list(profile: TVProfile) -> list[dict[str, Any]]:
    """Query the TV for its supported matte types.

    Returns a list of dicts, each with at least a ``matte_id`` key.
    The samsungtvws v3.x library handles both ``matte_type_list`` (modern)
    and ``matte_list`` (API 0.97) response keys internally.
    """
    result = _run_art_call(
        profile,
        lambda art: art.get_matte_list(),
        "Get matte list",
        priority="read",
    )
    # v3.x returns {"matte_types": [...], "matte_colors": [...]}
    if isinstance(result, dict):
        return result.get("matte_types", [])
    # Fallback for unexpected return types
    return result


def delete_art(profile: TVProfile, content_ids: list[str]) -> bool:
    """Delete artworks from the TV by content ID.

    Parameters
    ----------
    profile:
        TV connection profile.
    content_ids:
        List of content IDs to delete (e.g., ``["MY_F0006", "MY_F0007"]``).

    Returns
    -------
    True on success, False on failure.
    """
    def _do_delete() -> None:
        art = None
        try:
            art = _connect_art(profile)
            art.delete_list(content_ids)
        finally:
            with contextlib.suppress(Exception):
                art.close()

    try:
        _run_tv_op(profile, _do_delete, f"Delete {len(content_ids)} artwork(s)")
        logger.info("Deleted %d artwork(s): %s", len(content_ids), ", ".join(content_ids))
        return True
    except Exception as e:
        logger.error("Failed to delete artwork(s): %s", e)
        return False


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
    def _do_change_matte() -> None:
        art = None
        try:
            art = _connect_art(profile)
            art.change_matte(content_id, matte_id)
        finally:
            with contextlib.suppress(Exception):
                art.close()

    try:
        _run_tv_op(profile, _do_change_matte, f"Change matte on {content_id}")
        logger.info("Changed matte on %s to %s", content_id, matte_id)
        return True
    except Exception as e:
        logger.error("Failed to change matte: %s", e)
        return False
