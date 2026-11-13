"""Resolve rating targets from local records without changing television state."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from frameart.art_profile import load_art_quiz
from frameart.library import LibraryStore
from frameart.live_album import LiveAlbumStore
from frameart.tv.cache import TVCacheStore, tv_cache_key

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _text(value: Any, limit: int = 500) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _metadata(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _image_path(job_dir: Path) -> Path | None:
    for name in ("final.png", "source.png"):
        path = job_dir / name
        if path.is_file():
            return path
    return None


def _quiz_tags(source: str | None, artwork_id: str | None) -> list[str]:
    """Reuse editorial tags only for the same known museum work."""
    if not source or not artwork_id:
        return []
    expected_id = f"{source}-{artwork_id}"
    for pair in load_art_quiz()["pairs"]:
        for side in ("left", "right"):
            if pair[side].get("id") == expected_id:
                return pair[side].get("tags", [])
    return []


def _job_artwork(settings, job_dir: Path) -> dict:
    metadata = _metadata(job_dir / "meta.json")
    job_id = job_dir.name
    source_metadata = metadata.get("source_metadata")
    source_metadata = source_metadata if isinstance(source_metadata, dict) else {}
    source = _text(source_metadata.get("source"), 50)
    source_id = _text(source_metadata.get("artwork_id"), 200)
    key = f"job:{job_id}"
    if source in {"met", "aic", "cma", "europeana"} and source_id:
        key = f"catalog:{source}:{source_id}"
    extra = LibraryStore(settings.data_dir).metadata_for_jobs([job_id]).get(job_id, {})
    source_tags = source_metadata.get("tags", [])
    source_tags = source_tags if isinstance(source_tags, list) else []
    tags = list(dict.fromkeys(
        tag.strip().lower()[:100]
        for tag in [*extra.get("tags", []), *source_tags, *_quiz_tags(source, source_id)]
        if isinstance(tag, str) and tag.strip()
    ))[:40]
    image_path = _image_path(job_dir)
    if image_path is None:
        raise LookupError("Artwork image was not found.")
    stat = image_path.stat()
    version = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
    return {
        "key": key,
        "kind": "job",
        "job_id": job_id,
        "title": _text(source_metadata.get("title"))
        or _text(metadata.get("prompt_original")) or job_id,
        "artist": _text(source_metadata.get("artist"), 200),
        "style": _text(metadata.get("style"), 100),
        "source": source,
        "source_url": _text(source_metadata.get("source_url"), 2000),
        "tags": tags,
        "thumbnail_url": f"/jobs/{job_id}/thumbnail?v={version}",
        "reference": {"job_id": job_id},
    }


def _job_dirs(settings):
    return sorted((Path(settings.data_dir) / "artifacts").glob("*/*/*/*"), reverse=True)


def _album_artwork(settings, profile_id: str, content_id: str) -> dict | None:
    store = LiveAlbumStore(settings.data_dir)
    for album in store.list_albums():
        for item_id, item in store.current_items(album["id"], profile_id).items():
            if item["content_id"] != content_id:
                continue
            source = store.source_item(album["id"], item_id)
            digest = hashlib.sha256(f"{album['id']}\0{item_id}".encode()).hexdigest()
            preview = None
            if source and source["cache_path"].is_file():
                preview = f"/modes/live-album/{album['id']}/photos/{item_id}/image"
            return {
                "key": f"album:{digest}",
                "kind": "album",
                "title": _text(source.get("title")) if source else album["name"],
                "artist": None,
                "tags": [],
                "thumbnail_url": preview,
                "reference": {"tv_profile_id": profile_id, "content_id": content_id},
            }
    return None


def resolve_feedback_artwork(settings, reference: dict) -> dict:
    """Resolve a captured identifier, never the TV's potentially changed current image."""
    job_id = reference.get("job_id")
    if job_id:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ValueError("Invalid artwork job ID.")
        for job_dir in _job_dirs(settings):
            if job_dir.name == job_id and job_dir.is_dir():
                return _job_artwork(settings, job_dir)
        raise LookupError("Artwork job was not found.")

    profile_id = reference["tv_profile_id"]
    content_id = reference["content_id"]
    profile = settings.tvs.get(profile_id)
    if profile is None:
        raise LookupError("TV profile was not found.")
    for job_dir in _job_dirs(settings):
        if not job_dir.is_dir() or _image_path(job_dir) is None:
            continue
        metadata = _metadata(job_dir / "meta.json")
        tv_map = metadata.get("tv_content_ids")
        mapped_id = tv_map.get(profile.ip) if isinstance(tv_map, dict) else None
        matches = mapped_id == content_id or (
            metadata.get("tv_ip") == profile.ip and metadata.get("content_id") == content_id
        )
        if matches:
            return _job_artwork(settings, job_dir)

    album_artwork = _album_artwork(settings, profile_id, content_id)
    if album_artwork is not None:
        return album_artwork

    cached = TVCacheStore(settings.data_dir).get_thumbnail(tv_cache_key(profile), content_id)
    thumbnail_url = None
    if cached:
        query = urlencode({"tv_profile_id": profile_id, "content_id": content_id})
        thumbnail_url = f"/curation/thumbnail?{query}"
    return {
        "key": f"tv:{tv_cache_key(profile)}:{content_id}",
        "kind": "tv",
        "title": f"Artwork {content_id}",
        "artist": None,
        "tags": [],
        "thumbnail_url": thumbnail_url,
        "reference": {"tv_profile_id": profile_id, "content_id": content_id},
    }
