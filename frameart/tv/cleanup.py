"""TV artwork cleanup — delete old user-uploaded images from the Frame TV.

Only deletes artworks whose ``content_id`` starts with ``MY`` (user uploads).
Samsung Art Store items and built-in artwork are never touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from frameart.config import TVProfile
from frameart.tv.controller import _connect, list_art

logger = logging.getLogger(__name__)

# User-uploaded artwork content_ids start with these prefixes.
_USER_UPLOAD_PREFIXES = ("MY_F", "MY-F", "MY_C", "MY-C", "MY_")


def _is_user_upload(artwork: dict[str, Any]) -> bool:
    """Return True if the artwork was uploaded by a user (not Samsung Store)."""
    cid = artwork.get("content_id", "")
    return any(cid.startswith(p) for p in _USER_UPLOAD_PREFIXES)


def _is_favourite(artwork: dict[str, Any]) -> bool:
    """Return True if the artwork is marked as a favourite on the TV.

    Samsung firmware uses varying field names across models and versions,
    so we check several known possibilities.
    """
    for key in ("is_favourite", "favourite", "is_favorite", "favorite"):
        val = artwork.get(key)
        if val is not None:
            # Could be bool, str "true"/"false", or int 0/1
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes")
            return bool(val)
    # Some firmwares use category_id to indicate favourites
    cat = str(artwork.get("category_id", ""))
    return cat in ("MY-C0004", "MY_C0004")


@dataclass
class CleanupResult:
    """Result of a TV artwork cleanup operation."""

    deleted: list[str]
    kept: int
    skipped_favourites: int
    error: str | None = None


def cleanup_artworks(
    profile: TVProfile,
    *,
    keep: int = 20,
    delete_all: bool = False,
    order: str = "oldest_first",
    include_favourites: bool = False,
) -> CleanupResult:
    """Delete user-uploaded artworks from the TV to free space.

    Parameters
    ----------
    profile:
        TV connection profile.
    keep:
        Number of user-uploaded artworks to retain. Ignored when
        *delete_all* is True.
    delete_all:
        If True, delete **all** user-uploaded artworks (subject to the
        *include_favourites* flag).
    order:
        ``"oldest_first"`` (default) deletes the oldest uploads first,
        keeping the newest.  ``"newest_first"`` does the reverse.
    include_favourites:
        If False (default), artworks marked as favourites on the TV are
        never deleted.  Set to True to include them in cleanup.

    Returns
    -------
    CleanupResult with the list of deleted content_ids.
    """
    if order not in ("oldest_first", "newest_first"):
        return CleanupResult(
            deleted=[], kept=0, skipped_favourites=0,
            error=f"Invalid order '{order}'. Use 'oldest_first' or 'newest_first'.",
        )

    try:
        artworks = list_art(profile)
    except Exception as exc:
        return CleanupResult(
            deleted=[], kept=0, skipped_favourites=0,
            error=f"Failed to list artworks: {exc}",
        )

    # Filter to user uploads only
    user_artworks = [a for a in artworks if _is_user_upload(a)]
    logger.info(
        "Found %d total artworks, %d user-uploaded", len(artworks), len(user_artworks),
    )

    # Separate favourites
    favourites = [a for a in user_artworks if _is_favourite(a)]
    non_favourites = [a for a in user_artworks if not _is_favourite(a)]

    if include_favourites:
        candidates = user_artworks
        skipped_favourites = 0
    else:
        candidates = non_favourites
        skipped_favourites = len(favourites)
        if favourites:
            logger.info("Protecting %d favourite(s) from deletion", len(favourites))

    if not candidates:
        logger.info("No candidates for cleanup")
        return CleanupResult(deleted=[], kept=0, skipped_favourites=skipped_favourites)

    # Sort by content_id as a proxy for chronological order.
    # Samsung assigns sequential IDs (MY_F0001, MY_F0002, ...).
    candidates.sort(key=lambda a: a.get("content_id", ""))

    # Decide which to delete
    if delete_all:
        to_delete = candidates
    else:
        if len(candidates) <= keep:
            logger.info(
                "Only %d user artwork(s) on TV (keep=%d), nothing to delete",
                len(candidates), keep,
            )
            return CleanupResult(
                deleted=[], kept=len(candidates), skipped_favourites=skipped_favourites,
            )

        if order == "oldest_first":
            # Delete from the front (oldest), keep the tail (newest)
            to_delete = candidates[: len(candidates) - keep]
        else:
            # Delete from the tail (newest), keep the front (oldest)
            to_delete = candidates[keep:]

    ids_to_delete = [a["content_id"] for a in to_delete]
    kept_count = len(candidates) - len(to_delete)

    logger.info(
        "Deleting %d artwork(s), keeping %d (order=%s, include_favourites=%s)",
        len(ids_to_delete), kept_count, order, include_favourites,
    )

    try:
        tv = _connect(profile)
        art = tv.art()
        art.delete_list(ids_to_delete)
        logger.info("Deleted %d artwork(s): %s", len(ids_to_delete), ids_to_delete)
    except Exception as exc:
        return CleanupResult(
            deleted=[], kept=len(candidates), skipped_favourites=skipped_favourites,
            error=f"Failed to delete artworks: {exc}",
        )

    return CleanupResult(
        deleted=ids_to_delete, kept=kept_count, skipped_favourites=skipped_favourites,
    )
