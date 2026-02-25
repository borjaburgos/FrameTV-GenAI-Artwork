"""Public domain artwork adapters for external museum sources."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

MET_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"
AIC_API_BASE = "https://api.artic.edu/api/v1"
CMA_API_BASE = "https://openaccess-api.clevelandart.org/api"
RIJKS_SEARCH_API_BASE = "https://data.rijksmuseum.nl/search/collection"
RIJKS_ID_BASE = "https://id.rijksmuseum.nl"

logger = logging.getLogger(__name__)


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


def _rijks_artwork_id(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = raw.rstrip("/").split("/")[-1]
    return raw or None


def _rijks_id_url(artwork_id: str) -> str:
    return f"{RIJKS_ID_BASE}/{artwork_id}"


def _collect_strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_strings(item, out)
        return
    if isinstance(value, dict):
        for nested in value.values():
            _collect_strings(nested, out)


def _is_image_url(url: str) -> bool:
    lowered = url.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return False
    if "iiif.micr.io" in lowered:
        return True
    return any(
        token in lowered
        for token in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".jp2", "/iiif/")
    )


def _normalize_image_url(url: str) -> str:
    """Convert IIIF-related references into a directly downloadable image URL."""
    lowered = url.lower()
    if "iiif.micr.io" not in lowered:
        return url

    # info.json -> direct image
    if lowered.endswith("/info.json"):
        return url[: -len("/info.json")] + "/full/max/0/default.jpg"
    # manifest -> direct image
    if lowered.endswith("/manifest"):
        return url[: -len("/manifest")] + "/full/max/0/default.jpg"

    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path:
        return url
    last_segment = path.split("/")[-1]
    # Bare resource root like https://iiif.micr.io/RFwqO
    if "." not in last_segment and "/full/" not in path:
        base = url.rstrip("/")
        return f"{base}/full/max/0/default.jpg"
    return url


def _rijks_image_urls(*sources: dict[str, Any]) -> tuple[str | None, str | None]:
    strings: list[str] = []
    for source in sources:
        if isinstance(source, dict):
            _collect_strings(source, strings)
    image_urls: list[str] = []
    seen: set[str] = set()
    for value in strings:
        if value in seen:
            continue
        seen.add(value)
        if _is_image_url(value):
            image_urls.append(_normalize_image_url(value))
    if not image_urls:
        return None, None
    return image_urls[0], image_urls[0]


def _rijks_artists(summary_obj: dict[str, Any]) -> str | None:
    artists = summary_obj.get("artists")
    if isinstance(artists, list):
        names: list[str] = []
        for artist in artists:
            if isinstance(artist, str) and artist:
                names.append(artist)
            elif isinstance(artist, dict):
                name = artist.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
        if names:
            return ", ".join(dict.fromkeys(names))
    return None


def _rijks_date(summary_obj: dict[str, Any]) -> str | None:
    date = summary_obj.get("dating")
    if isinstance(date, str) and date:
        return date
    if isinstance(date, dict):
        for key in ("label", "name", "period", "year"):
            value = date.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _rijks_object_to_item(
    artwork_id: str,
    summary_obj: dict[str, Any],
    linked_art_obj: dict[str, Any],
) -> dict[str, Any] | None:
    image_url, thumb_url = _rijks_image_urls(linked_art_obj, summary_obj)
    if not image_url:
        return None

    title = summary_obj.get("title")
    if not isinstance(title, str) or not title:
        title = linked_art_obj.get("_label")
    if not isinstance(title, str) or not title:
        title = f"Rijksmuseum Artwork {artwork_id}"

    source_url = linked_art_obj.get("id")
    if not isinstance(source_url, str) or not source_url:
        source_url = _rijks_id_url(artwork_id)

    return {
        "source": "rijks",
        "artwork_id": artwork_id,
        "title": title,
        "artist": _rijks_artists(summary_obj),
        "date": _rijks_date(summary_obj),
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
    url = _rijks_id_url(artwork_id)
    resp = client.get(url, headers={"Accept": "application/ld+json"})
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        fallback = client.get(
            url,
            params={"_profile": "la", "_mediatype": "application/ld+json"},
            headers={"Accept": "application/ld+json"},
        )
        fallback.raise_for_status()
        data = fallback.json()
    return data if isinstance(data, dict) else {}


def _rijks_search_candidates(
    client: httpx.Client,
    query: str,
    max_candidates: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Search Rijks by supported fields and return unique object IDs + summaries."""
    seen: set[str] = set()
    candidates: list[tuple[str, dict[str, Any]]] = []
    last_error: Exception | None = None
    query_variants = [
        {"query": query},
        {"title": query},
        {"creator": query},
        {"description": query},
        {"type": query},
        {"q": query},
    ]

    for params in query_variants:
        try:
            resp = client.get(RIJKS_SEARCH_API_BASE, params=params)
            resp.raise_for_status()
            payload = resp.json()
            logger.info(
                "Rijks search params=%s status=%s",
                params,
                resp.status_code,
            )
        except Exception as exc:
            last_error = exc
            logger.warning("Rijks search request failed params=%s error=%s", params, exc)
            continue

        items: list[Any] = []
        if isinstance(payload, dict):
            ordered = payload.get("orderedItems")
            results = payload.get("results")
            generic_items = payload.get("items")
            logger.info(
                "Rijks payload keys=%s orderedItems=%s results=%s items=%s",
                sorted(payload.keys()),
                len(ordered) if isinstance(ordered, list) else "n/a",
                len(results) if isinstance(results, list) else "n/a",
                len(generic_items) if isinstance(generic_items, list) else "n/a",
            )
            if isinstance(ordered, list):
                items = ordered
            elif isinstance(results, list):
                items = results
            elif isinstance(generic_items, list):
                items = generic_items

        for raw_item in items:
            if isinstance(raw_item, dict):
                summary = raw_item
                raw_id = raw_item.get("id") or raw_item.get("@id")
            elif isinstance(raw_item, str):
                summary = {}
                raw_id = raw_item
            else:
                continue

            artwork_id = _rijks_artwork_id(raw_id)
            if not artwork_id or artwork_id in seen:
                continue

            seen.add(artwork_id)
            candidates.append((artwork_id, summary))
            if len(candidates) >= max_candidates:
                logger.info("Rijks candidate cap reached count=%d", len(candidates))
                return candidates

    if not candidates and last_error:
        logger.warning("Rijks search produced no candidates and last_error=%s", last_error)
        raise last_error
    logger.info(
        "Rijks search candidates query=%r count=%d sample_ids=%s",
        query,
        len(candidates),
        [cid for cid, _ in candidates[:5]],
    )
    return candidates


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

        # Rijksmuseum Data Services (keyless)
        data = _rijks_search_candidates(client, q, max(limit * 4, 40))
        results: list[dict[str, Any]] = []
        dropped_no_image = 0
        dropped_detail_error = 0
        for artwork_id, summary in data:
            detail: dict[str, Any] = {}
            try:
                detail = _rijks_fetch_object(client, artwork_id)
            except Exception:
                dropped_detail_error += 1
                detail = {}
            item = _rijks_object_to_item(artwork_id, summary, detail)
            if not item:
                dropped_no_image += 1
                continue
            results.append(item)
            if len(results) >= limit:
                break
        logger.info(
            "Rijks final query=%r candidates=%d returned=%d dropped_no_image=%d detail_errors=%d",
            q,
            len(data),
            len(results),
            dropped_no_image,
            dropped_detail_error,
        )
        return results


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
            normalized_id = _rijks_artwork_id(artwork_id)
            if not normalized_id:
                raise ValueError("Invalid Rijksmuseum artwork ID.")
            obj = _rijks_fetch_object(client, normalized_id)
            item = _rijks_object_to_item(normalized_id, {}, obj)

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
