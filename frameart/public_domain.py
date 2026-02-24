"""Public domain artwork adapters for external museum sources."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx

MET_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"
AIC_API_BASE = "https://api.artic.edu/api/v1"
CMA_API_BASE = "https://openaccess-api.clevelandart.org/api"
RIJKS_API_BASE = "https://www.rijksmuseum.nl/api/en/collection"


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


def _rijks_api_key() -> str:
    key = os.environ.get("FRAMEART_RIJKS_API_KEY") or os.environ.get("RIJKS_API_KEY")
    if key:
        return key
    raise ValueError(
        "Rijksmuseum API key not configured. Set FRAMEART_RIJKS_API_KEY (or RIJKS_API_KEY)."
    )


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


def _cma_image_urls(obj: dict[str, Any]) -> tuple[str | None, str | None]:
    images = obj.get("images") if isinstance(obj.get("images"), dict) else {}
    order = ["web", "print", "full", "archive"]
    urls: list[str] = []
    for key in order:
        value = images.get(key)
        if isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    if not urls:
        top_url = obj.get("image_url")
        if isinstance(top_url, str) and top_url:
            urls.append(top_url)
    if not urls:
        return None, None
    image_url = urls[0]
    thumb_url = urls[0]
    return image_url, thumb_url


def _cma_is_public_domain(obj: dict[str, Any]) -> bool:
    open_access = obj.get("open_access")
    if isinstance(open_access, bool) and open_access:
        return True
    if isinstance(open_access, int) and open_access == 1:
        return True

    status = str(obj.get("share_license_status") or "").upper()
    if "CC0" in status:
        return True

    rights = str(obj.get("rights_type") or obj.get("copyright") or "").lower()
    return bool("public domain" in rights or "cc0" in rights)


def _cma_object_to_item(obj: dict[str, Any]) -> dict[str, Any] | None:
    if not _cma_is_public_domain(obj):
        return None

    image_url, thumb_url = _cma_image_urls(obj)
    if not image_url:
        return None

    artwork_id = str(obj.get("id", ""))
    if not artwork_id:
        return None

    creators = obj.get("creators") if isinstance(obj.get("creators"), list) else []
    artist_names: list[str] = []
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        desc = creator.get("description")
        if isinstance(desc, str) and desc:
            artist_names.append(desc)
    artist = ", ".join(dict.fromkeys(artist_names)) if artist_names else None

    source_url = obj.get("url")
    if not isinstance(source_url, str) or not source_url:
        source_url = f"https://www.clevelandart.org/art/{artwork_id}"

    return {
        "source": "cma",
        "artwork_id": artwork_id,
        "title": obj.get("title") or f"CMA Artwork {artwork_id}",
        "artist": artist,
        "date": obj.get("creation_date") or None,
        "image_url": image_url,
        "thumbnail_url": thumb_url,
        "license": obj.get("share_license_status") or "Open Access",
        "attribution": "Cleveland Museum of Art",
        "source_url": source_url,
        "is_public_domain": True,
    }


def _rijks_image_urls(obj: dict[str, Any]) -> tuple[str | None, str | None]:
    web_image = obj.get("webImage") if isinstance(obj.get("webImage"), dict) else {}
    header_image = obj.get("headerImage") if isinstance(obj.get("headerImage"), dict) else {}
    image_url = web_image.get("url") if isinstance(web_image.get("url"), str) else None
    thumb_url = header_image.get("url") if isinstance(header_image.get("url"), str) else None
    if image_url and not thumb_url:
        thumb_url = image_url
    return image_url, thumb_url


def _rijks_object_to_item(obj: dict[str, Any]) -> dict[str, Any] | None:
    artwork_id = str(obj.get("objectNumber", "")).strip()
    if not artwork_id:
        return None

    image_url, thumb_url = _rijks_image_urls(obj)
    if not image_url:
        return None

    principal = obj.get("principalMaker")
    if not isinstance(principal, str) or not principal:
        principal = obj.get("principalOrFirstMaker")
    artist = principal if isinstance(principal, str) and principal else None

    dating = obj.get("dating") if isinstance(obj.get("dating"), dict) else {}
    date = dating.get("presentingDate")
    if not isinstance(date, str) or not date:
        date = obj.get("longTitle")
    if not isinstance(date, str) or not date:
        date = None

    links = obj.get("links") if isinstance(obj.get("links"), dict) else {}
    source_url = links.get("web")
    if not isinstance(source_url, str) or not source_url:
        source_url = f"https://www.rijksmuseum.nl/en/collection/{artwork_id}"

    return {
        "source": "rijks",
        "artwork_id": artwork_id,
        "title": obj.get("title") or f"Rijksmuseum Artwork {artwork_id}",
        "artist": artist,
        "date": date,
        "image_url": image_url,
        "thumbnail_url": thumb_url,
        "license": "Public Domain",
        "attribution": "Rijksmuseum",
        "source_url": source_url,
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


def _cma_fetch_object(client: httpx.Client, artwork_id: str) -> dict[str, Any]:
    resp = client.get(f"{CMA_API_BASE}/artworks/{artwork_id}")
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {})


def _rijks_fetch_object(client: httpx.Client, artwork_id: str) -> dict[str, Any]:
    key = _rijks_api_key()
    resp = client.get(f"{RIJKS_API_BASE}/{artwork_id}", params={"key": key})
    resp.raise_for_status()
    return resp.json().get("artObject", {})


def search_artworks(source: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search public-domain artworks for a source and query."""
    q = query.strip()
    if not q:
        return []

    src = source.lower()
    if src not in {"met", "aic", "cma", "rijks"}:
        raise ValueError(f"Unsupported source '{source}'. Use 'met', 'aic', 'cma', or 'rijks'.")

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

        if src == "aic":
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

        if src == "cma":
            resp = client.get(
                f"{CMA_API_BASE}/artworks",
                params={
                    "q": q,
                    "has_image": "1",
                    "limit": min(max(limit, 1), 50),
                },
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
            items = [_cma_object_to_item(obj) for obj in data if isinstance(obj, dict)]
            return [item for item in items if item is not None][:limit]

        # Rijksmuseum
        key = _rijks_api_key()
        resp = client.get(
            RIJKS_API_BASE,
            params={
                "key": key,
                "q": q,
                "imgonly": "True",
                "ps": min(max(limit, 1), 100),
                "s": "relevance",
            },
        )
        resp.raise_for_status()
        data = resp.json().get("artObjects") or []
        items = [_rijks_object_to_item(obj) for obj in data if isinstance(obj, dict)]
        return [item for item in items if item is not None][:limit]


def get_artwork(source: str, artwork_id: str) -> dict[str, Any]:
    """Fetch a single artwork metadata record by source and ID."""
    src = source.lower()
    if src not in {"met", "aic", "cma", "rijks"}:
        raise ValueError(f"Unsupported source '{source}'. Use 'met', 'aic', 'cma', or 'rijks'.")

    with _http_client() as client:
        if src == "met":
            obj = _met_fetch_object(client, artwork_id)
            item = _met_object_to_item(obj)
        elif src == "aic":
            obj = _aic_fetch_object(client, artwork_id)
            item = _aic_object_to_item(obj)
        elif src == "cma":
            obj = _cma_fetch_object(client, artwork_id)
            item = _cma_object_to_item(obj)
        else:
            obj = _rijks_fetch_object(client, artwork_id)
            item = _rijks_object_to_item(obj)

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
