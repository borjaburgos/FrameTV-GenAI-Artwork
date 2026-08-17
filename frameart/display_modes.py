"""Shared bounded TV-image replacement helpers for live display modes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from frameart.automation import AutomationStore

logger = logging.getLogger(__name__)


def replace_group_image(settings, mode: dict[str, Any], image_path: Path):
    """Upload, switch, and retire the prior image for every TV in a group.

    The returned state keeps at most one current content ID and ten failed-delete
    IDs per TV. A newly uploaded image is also removed when switching fails.
    """
    from frameart.tv.controller import delete_art, switch_art, upload_image

    group = AutomationStore(settings.data_dir).get_group(mode["group_id"])
    if not group:
        return mode["current_content_ids"], mode["stale_content_ids"], [], [
            "Configured TV group no longer exists."
        ]
    current = dict(mode["current_content_ids"])
    stale = {key: list(value) for key, value in mode["stale_content_ids"].items()}
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    image_bytes = Path(image_path).read_bytes()
    for profile_id in group["tv_profile_ids"]:
        profile = settings.tvs.get(profile_id)
        if profile is None:
            errors.append(f"{profile_id}: TV profile is no longer configured")
            continue
        retry_ids = stale.pop(profile_id, [])
        if retry_ids:
            try:
                if not delete_art(profile, retry_ids):
                    stale[profile_id] = retry_ids[-10:]
            except Exception:
                stale[profile_id] = retry_ids[-10:]
        new_content_id: str | None = None
        try:
            uploaded = upload_image(profile, image_bytes, file_type="PNG", matte="none")
            if not uploaded.success or not uploaded.content_id:
                raise RuntimeError(uploaded.error or "TV upload failed")
            new_content_id = uploaded.content_id
            if not switch_art(profile, new_content_id):
                raise RuntimeError("TV did not switch to the new image")
            old_id = current.get(profile_id)
            current[profile_id] = new_content_id
            if old_id and old_id != new_content_id:
                try:
                    if not delete_art(profile, [old_id]):
                        stale.setdefault(profile_id, []).append(old_id)
                except Exception:
                    stale.setdefault(profile_id, []).append(old_id)
            results.append({"tv_profile_id": profile_id, "content_id": new_content_id})
        except Exception as exc:
            if new_content_id and current.get(profile_id) != new_content_id:
                try:
                    if not delete_art(profile, [new_content_id]):
                        stale.setdefault(profile_id, []).append(new_content_id)
                except Exception:
                    stale.setdefault(profile_id, []).append(new_content_id)
            errors.append(f"{profile_id}: {exc}")
    for profile_id in list(stale):
        stale[profile_id] = list(dict.fromkeys(stale[profile_id]))[-10:]
    return current, stale, results, errors


def delete_mode_tv_images(settings, mode: dict[str, Any]) -> None:
    """Best-effort cleanup of current and retry-queued content for a mode."""
    from frameart.tv.controller import delete_art

    group = AutomationStore(settings.data_dir).get_group(mode["group_id"])
    profile_ids = group["tv_profile_ids"] if group else []
    for profile_id in profile_ids:
        profile = settings.tvs.get(profile_id)
        content_ids = [mode["current_content_ids"].get(profile_id)]
        content_ids.extend(mode["stale_content_ids"].get(profile_id, []))
        content_ids = list(dict.fromkeys(item for item in content_ids if item))
        if profile and content_ids:
            try:
                delete_art(profile, content_ids)
            except Exception:
                logger.warning("Could not clean up live-mode TV art for %s", profile_id)
