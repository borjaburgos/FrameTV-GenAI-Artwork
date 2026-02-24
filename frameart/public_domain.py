"""Public domain artwork adapters for external museum sources."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

MET_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"
AIC_API_BASE = "https://api.artic.edu/api/v1"


def _http_client() -> httpx.Client:
    return httpx.Client(
        timeout=20.0,
        follow_redirects=True,
        headers={
            "User-Agent": "FrameArt/0.1 (+https://github.com/borjaburgos/FrameTV-GenAI-Artwork)",
            "Accept": "*/*",
            "Referer": "https://www.metmuseum.org/",
        },
    )


def _safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._") or "item"


def _met_object_to_item(obj: dict[str, Any]) -> dict[str, Any] | None:
    if not obj.get("isPublicDomain"):
        return None

    image_url = obj.get("primaryImage") or obj.get("primaryImageSmall")
    thumb_url = obj.get("primaryImageSmall") or obj.get("primaryImage")
    if not image_url:
        return None

    artwork_id = str(obj.get("objectID", ""))
    if not artwork_id:
        return None

    return {
        "source": "met",
        "artwork_id": artwork_id,
        "title": obj.get("title") or f"Met Object {artwork_id}",
        "artist": obj.get("artistDisplayName") or None,
        "date": obj.get("objectDate") or None,
        "image_url": image_url,
        "thumbnail_url": thumb_url,
        "license": "Public Domain (CC0)",
        "attribution": "The Metropolitan Museum of Art",
        "source_url": obj.get("objectURL")
        or f"https://www.metmuseum.org/art/collection/search/{artwork_id}",
        "is_public_domain": True,
    }


def _aic_object_to_item(obj: dict[str, Any]) -> dict[str, Any] | None:
    if not obj.get("is_public_domain"):
        return None

    image_id = obj.get("image_id")
    if not image_id:
        return None

    artwork_id = str(obj.get("id", ""))
    if not artwork_id:
        return None

    return {
        "source": "aic",
        "artwork_id": artwork_id,
        "title": obj.get("title") or f"AIC Artwork {artwork_id}",
        "artist": obj.get("artist_title") or None,
        "date": obj.get("date_display") or None,
        "image_url": f"https://www.artic.edu/iiif/2/{image_id}/full/1686,/0/default.jpg",
        "thumbnail_url": f"https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg",
        "license": "Public Domain",
        "attribution": "Art Institute of Chicago",
        "source_url": f"https://www.artic.edu/artworks/{artwork_id}",
        "is_public_domain": True,
    }


def _met_fetch_object(client: httpx.Client, artwork_id: str) -> dict[str, Any]:
    resp = client.get(f"{MET_API_BASE}/objects/{artwork_id}")
    resp.raise_for_status()
    return resp.json()


def _aic_fetch_object(client: httpx.Client, artwork_id: str) -> dict[str, Any]:
    fields = ",".join(
        [
            "id",
            "title",
            "artist_title",
            "date_display",
            "is_public_domain",
            "image_id",
        ]
    )
    resp = client.get(f"{AIC_API_BASE}/artworks/{artwork_id}", params={"fields": fields})
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {})


def search_artworks(source: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search public-domain artworks for a source and query."""
    q = query.strip()
    if not q:
        return []

    src = source.lower()
    if src not in {"met", "aic"}:
        raise ValueError(f"Unsupported source '{source}'. Use 'met' or 'aic'.")

    with _http_client() as client:
        if src == "met":
            search_resp = client.get(
                f"{MET_API_BASE}/search",
                params={"q": q, "hasImages": "true"},
            )
            search_resp.raise_for_status()
            object_ids = list(search_resp.json().get("objectIDs") or [])
            results: list[dict[str, Any]] = []
            for obj_id in object_ids[: max(limit * 4, 40)]:
                try:
                    obj = _met_fetch_object(client, str(obj_id))
                except Exception:
                    continue
                item = _met_object_to_item(obj)
                if not item:
                    continue
                results.append(item)
                if len(results) >= limit:
                    break
            return results

        fields = ",".join(
            [
                "id",
                "title",
                "artist_title",
                "date_display",
                "is_public_domain",
                "image_id",
            ]
        )
        resp = client.get(
            f"{AIC_API_BASE}/artworks/search",
            params={
                "q": q,
                "limit": min(max(limit, 1), 50),
                "fields": fields,
            },
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        items = [_aic_object_to_item(obj) for obj in data]
        return [item for item in items if item is not None][:limit]


def get_artwork(source: str, artwork_id: str) -> dict[str, Any]:
    """Fetch a single artwork metadata record by source and ID."""
    src = source.lower()
    if src not in {"met", "aic"}:
        raise ValueError(f"Unsupported source '{source}'. Use 'met' or 'aic'.")

    with _http_client() as client:
        if src == "met":
            obj = _met_fetch_object(client, artwork_id)
            item = _met_object_to_item(obj)
        else:
            obj = _aic_fetch_object(client, artwork_id)
            item = _aic_object_to_item(obj)

    if not item:
        raise ValueError("Artwork is unavailable or not public domain.")
    return item


def download_artwork_image(
    source: str,
    artwork_id: str,
    dest_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """Download a public-domain artwork image to disk."""
    item = get_artwork(source, artwork_id)
    image_url = item["image_url"]
    safe_name = _safe_filename(f"{item['source']}_{item['artwork_id']}.jpg")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / safe_name

    with _http_client() as client:
        resp = client.get(image_url)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)

    return out_path, item
