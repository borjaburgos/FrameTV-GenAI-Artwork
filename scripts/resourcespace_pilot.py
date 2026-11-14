#!/usr/bin/env python3
"""Validate/import exactly 20 full-frame TV renditions; never write NAS originals.

Dry-run is the default and makes no network requests. --apply requires a new,
dedicated ResourceSpace pilot instance and a bootstrap/admin API identity. Runtime
FrameArt uses a different, read-only identity; this writer is deliberately separate.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx2 as httpx
from PIL import Image
from pydantic import ValidationError

from frameart.art_catalog import ArtworkMetadata
from frameart.resourcespace import canonical_field_name

PILOT_SIZE = 20
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
SHA256_PATTERN = re.compile(r"[a-fA-F0-9]{64}\Z")
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
METADATA_FIELDS = {
    "frameart_id": 0,
    "frameart_metadata": 5,
    "frameart_artist": 0,
    "frameart_artist_bio": 1,
    "frameart_date": 0,
    "frameart_medium": 0,
    "frameart_license": 0,
    "frameart_source_url": 0,
}


class PilotError(ValueError):
    """A safe, actionable pilot validation/import failure."""


@dataclass(frozen=True)
class PilotItem:
    metadata: dict[str, Any]
    original: Path
    rendition: Path
    original_relative: str
    rendition_relative: str
    original_sha256: str
    rendition_sha256: str


@dataclass(frozen=True)
class PilotManifest:
    collection_name: str
    items: tuple[PilotItem, ...]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def contained_file(root: Path, relative: Any, label: str) -> tuple[Path, str]:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise PilotError(f"{label} must be a relative file path")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PilotError(f"{label} cannot escape its configured root")
    try:
        resolved = (root / candidate).resolve(strict=True)
    except OSError as exc:
        raise PilotError(f"{label} is missing; check the NAS mount and manifest") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise PilotError(f"{label} must resolve to a regular file inside its root")
    return resolved, candidate.as_posix()


def checked_hash(path: Path, expected: Any, label: str) -> str:
    if not isinstance(expected, str) or not SHA256_PATTERN.fullmatch(expected):
        raise PilotError(f"{label} requires a SHA-256 checksum")
    actual = file_sha256(path)
    if actual != expected.lower():
        raise PilotError(f"{label} checksum mismatch; no import was attempted")
    return actual


def load_manifest(path: Path, originals_root: Path, renditions_root: Path) -> PilotManifest:
    """Read and validate every file before allowing any remote writes."""
    roots: list[Path] = []
    for root, label in ((originals_root, "originals"), (renditions_root, "renditions")):
        if not root.is_absolute():
            raise PilotError(f"The {label} root must be an absolute existing directory")
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise PilotError(f"The {label} root is unavailable; check its mount") from exc
        if not resolved.is_dir() or resolved == Path("/"):
            raise PilotError(f"The {label} root must be a specific existing directory")
        roots.append(resolved)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise PilotError("Pilot manifest is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise PilotError("Pilot manifest requires schema_version 1")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != PILOT_SIZE:
        raise PilotError("The pilot requires exactly 20 artworks")
    collection_name = data.get("collection_name", "FrameArt — 20 artwork pilot")
    if not isinstance(collection_name, str) or not 1 <= len(collection_name.strip()) <= 200:
        raise PilotError("A nonempty collection_name of at most 200 characters is required")
    items: list[PilotItem] = []
    ids: set[str] = set()
    rendition_paths: set[Path] = set()
    for raw in raw_items:
        if not isinstance(raw, dict) or not isinstance(raw.get("metadata"), dict):
            raise PilotError("Every item requires an artwork metadata object")
        metadata = copy.deepcopy(raw["metadata"])
        artwork_id = metadata.get("id")
        if not isinstance(artwork_id, str) or not ID_PATTERN.fullmatch(artwork_id):
            raise PilotError("Artwork IDs must be safe identifiers of at most 120 characters")
        if artwork_id in ids:
            raise PilotError("The pilot contains duplicate artwork IDs")
        ids.add(artwork_id)
        artist = metadata.get("artist")
        if not isinstance(metadata.get("title"), str) or not metadata["title"].strip():
            raise PilotError(f"{artwork_id}: title is required")
        if not isinstance(artist, dict) or not isinstance(artist.get("name"), str):
            raise PilotError(f"{artwork_id}: artist.name is required")
        if not isinstance(artist.get("id"), str) or not artist["id"].strip():
            raise PilotError(f"{artwork_id}: a stable artist.id is required")
        if not artist["name"].strip() or not isinstance(metadata.get("ai_generated"), bool):
            raise PilotError(
                f"{artwork_id}: artist name and explicit ai_generated flag are required"
            )
        if not isinstance(metadata.get("license"), str) or not metadata["license"].strip():
            raise PilotError(
                f"{artwork_id}: license is required; do not infer public-domain status"
            )
        if metadata.get("width") != 3840 or metadata.get("height") != 2160:
            raise PilotError(f"{artwork_id}: metadata must describe the 3840×2160 TV rendition")
        original, original_relative = contained_file(roots[0], raw.get("original_path"), "original")
        rendition, rendition_relative = contained_file(
            roots[1], raw.get("rendition_path"), "rendition"
        )
        if original == rendition or rendition in rendition_paths:
            raise PilotError("Each rendition must be unique and separate from its original")
        rendition_paths.add(rendition)
        if rendition.stat().st_size > MAX_IMAGE_BYTES:
            raise PilotError(f"{artwork_id}: TV rendition exceeds 50 MiB")
        with Image.open(rendition) as image:
            if image.format != "JPEG" or image.size != (3840, 2160) or image.mode != "RGB":
                raise PilotError(f"{artwork_id}: TV rendition must be a 3840×2160 RGB JPEG")
            image.verify()
        original_hash = checked_hash(original, raw.get("original_sha256"), "original")
        rendition_hash = checked_hash(rendition, raw.get("rendition_sha256"), "rendition")
        metadata.update(
            {
                "schema_version": 1,
                "original_path": original_relative,
                "original_sha256": original_hash,
                "rendition_sha256": rendition_hash,
            }
        )
        if isinstance(metadata.get("ai_provenance"), (dict, list)):
            metadata["ai_provenance"] = json.dumps(metadata["ai_provenance"], ensure_ascii=False)
        try:
            validated = ArtworkMetadata.model_validate(metadata)
        except ValidationError:
            raise PilotError(
                f"{artwork_id}: metadata does not match FrameArt's artwork schema"
            ) from None
        # Retain portable file/crop provenance beyond the UI's typed fields, but
        # normalize all fields the running reader will validate before any writes.
        metadata.update(validated.model_dump(mode="json", exclude_none=True))
        if len(json.dumps(metadata, ensure_ascii=False)) > 50_000:
            raise PilotError(f"{artwork_id}: artwork metadata is too large")
        item = PilotItem(
            metadata,
            original,
            rendition,
            original_relative,
            rendition_relative,
            original_hash,
            rendition_hash,
        )
        field_values(item)
        items.append(item)
    return PilotManifest(collection_name.strip(), tuple(items))


class AdminAPI:
    """Explicit bootstrap writer; never used by the running FrameArt server."""

    def __init__(self, url: str, user: str, api_key: str):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PilotError(
                "ResourceSpace URL must be HTTP(S), without credentials/query/fragment"
            )
        if not user or not api_key:
            raise PilotError("A bootstrap username and API key are required for --apply")
        self.endpoint = url.rstrip("/") + "/api/"
        self.user = user
        self.api_key = api_key
        self.client = httpx.Client(timeout=120.0, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self.client.close()

    def call(self, function: str, *, upload: Path | None = None, **params: Any) -> Any:
        values = {"user": self.user, "function": function, **params}
        values = {
            key: str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in values.items()
        }
        query = urlencode(values)
        signature = hashlib.sha256((self.api_key + query).encode()).hexdigest()
        form = {"query": query + "&sign=" + signature, "sign": signature, "user": self.user}
        try:
            if upload is None:
                response = self.client.post(self.endpoint, data=form)
            else:
                with upload.open("rb") as source:
                    response = self.client.post(
                        self.endpoint,
                        data=form,
                        files={"userfile": (upload.name, source, "image/jpeg")},
                    )
            response.raise_for_status()
            if len(response.content) > MAX_MANIFEST_BYTES:
                raise PilotError(f"ResourceSpace {function} returned an oversized response")
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Do not print response bodies, signed URLs, keys, or credentials.
            raise PilotError(
                f"ResourceSpace {function} request failed; check service/permissions"
            ) from exc
        if (
            result is False
            or result is None
            or (isinstance(result, dict) and result.get("status") in {"fail", "error"})
            or (isinstance(result, str) and result.startswith("FAILED"))
        ):
            raise PilotError(f"ResourceSpace {function} refused the operation")
        return result


def positive_ref(value: Any, label: str) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise PilotError(f"ResourceSpace returned an invalid {label}")
    return int(value)


def result_rows(value: Any) -> list[dict[str, Any]]:
    rows = value.get("data") if isinstance(value, dict) else value
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PilotError("ResourceSpace returned an invalid result list")
    return rows


def resource_fields(api: AdminAPI, resource: int) -> dict[str, str]:
    rows = result_rows(api.call("get_resource_field_data", resource=resource))
    result = {}
    for row in rows:
        name = canonical_field_name(str(row.get("name")))
        if name in result:
            raise PilotError("ResourceSpace contains ambiguous FrameArt metadata fields")
        result[name] = str(row.get("value") or "")
    return result


def ensure_fields(api: AdminAPI) -> tuple[dict[str, int], str]:
    rows = result_rows(api.call("get_resource_type_fields"))
    # ResourceSpace's creator removes underscores/hyphens from shortnames.
    # Resolve both actual API names and manually configured canonical names,
    # but never silently choose between duplicate definitions.
    fields = {}
    actual_names = {}
    for row in rows:
        actual_name = str(row.get("name"))
        name = canonical_field_name(actual_name)
        if name in fields:
            raise PilotError("ResourceSpace contains ambiguous FrameArt metadata fields")
        fields[name] = row
        actual_names[name] = actual_name
    if "title" not in fields:
        raise PilotError("Default ResourceSpace title field is missing; finish setup first")
    refs = {"title": positive_ref(fields["title"]["ref"], "title field")}
    if "caption" in fields:
        refs["caption"] = positive_ref(fields["caption"]["ref"], "caption field")
    for name, field_type in METADATA_FIELDS.items():
        if name in fields:
            if int(fields[name].get("type", -1)) != field_type:
                raise PilotError(f"Existing {name} field has an incompatible type")
            refs[name] = positive_ref(fields[name]["ref"], "metadata field")
        else:
            created = api.call(
                "create_resource_type_field", name=name, resource_types="1", type=field_type
            )
            if not isinstance(created, dict) or not isinstance(created.get("data"), dict):
                raise PilotError("ResourceSpace did not confirm metadata field creation")
            refs[name] = positive_ref(created["data"].get("ref"), "new metadata field")
            actual_names[name] = name.replace("_", "")
    return refs, actual_names["frameart_id"]


def ensure_collection(api: AdminAPI, name: str) -> int:
    matches = [
        row for row in result_rows(api.call("get_user_collections")) if row.get("name") == name
    ]
    if len(matches) > 1:
        raise PilotError("Multiple pilot collections have this name; resolve the ambiguity first")
    if matches:
        return positive_ref(matches[0].get("ref"), "collection")
    return positive_ref(api.call("create_collection", name=name), "new collection")


def field_values(item: PilotItem) -> dict[str, str]:
    metadata = item.metadata
    values = {
        "frameart_id": metadata["id"],
        "frameart_metadata": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        "title": metadata["title"],
        "caption": metadata.get("description") or "",
        "frameart_artist": metadata["artist"]["name"],
        "frameart_artist_bio": metadata["artist"].get("bio") or "",
        "frameart_date": metadata.get("date") or "",
        "frameart_medium": metadata.get("medium") or "",
        "frameart_license": metadata["license"],
        "frameart_source_url": metadata.get("source_url") or "",
    }
    if any(not isinstance(value, str) for value in values.values()):
        raise PilotError("Readable metadata values must be strings")
    return values


def import_pilot(manifest: PilotManifest, api: AdminAPI) -> dict[str, Any]:
    """Idempotently upload renditions, refusing to overwrite a different artwork."""
    fields, id_shortname = ensure_fields(api)
    collection = ensure_collection(api, manifest.collection_name)
    current = result_rows(
        api.call("do_search", search=f"!collection{collection}", fetchrows="0,21", archive="0")
    )
    if len(current) > PILOT_SIZE:
        raise PilotError("Pilot collection already contains more than 20 resources")
    allowed_ids = {item.metadata["id"] for item in manifest.items}
    existing: dict[str, int] = {}
    for row in current:
        ref = positive_ref(row.get("ref"), "resource")
        artwork_id = resource_fields(api, ref).get("frameart_id")
        if artwork_id not in allowed_ids or artwork_id in existing:
            raise PilotError(
                "Pilot collection contains foreign or duplicate artworks; import stopped"
            )
        existing[artwork_id] = ref
    imported: list[dict[str, Any]] = []
    for item in manifest.items:
        # Files are rechecked immediately before each write, not merely during dry-run.
        checked_hash(item.original, item.original_sha256, "original")
        checked_hash(item.rendition, item.rendition_sha256, "rendition")
        values = field_values(item)
        artwork_id = item.metadata["id"]
        ref = existing.get(artwork_id)
        if ref is None:
            # Recover resources created before a prior interrupted collection-add.
            candidates = result_rows(
                api.call(
                    "do_search",
                    search=f"{id_shortname}:{artwork_id}",
                    fetchrows="0,21",
                    archive="0",
                )
            )
            exact = []
            for candidate in candidates:
                candidate_ref = positive_ref(candidate.get("ref"), "resource")
                if resource_fields(api, candidate_ref).get("frameart_id") == artwork_id:
                    exact.append(candidate_ref)
            if len(exact) > 1:
                raise PilotError(f"{artwork_id}: ambiguous existing resources; import stopped")
            if exact:
                ref = exact[0]
            else:
                initial = {
                    str(fields[name]): value for name, value in values.items() if name in fields
                }
                ref = positive_ref(
                    api.call(
                        "create_resource",
                        resource_type=1,
                        archive=0,
                        metadata=json.dumps(initial, ensure_ascii=False),
                    ),
                    "new resource",
                )
        prior = resource_fields(api, ref)
        try:
            stored = json.loads(prior.get("frameart_metadata") or "{}")
        except json.JSONDecodeError as exc:
            raise PilotError(f"{artwork_id}: existing resource metadata is invalid") from exc
        if (
            not isinstance(stored, dict)
            or stored.get("id") != artwork_id
            or stored.get("original_sha256") != item.original_sha256
            or stored.get("rendition_sha256") != item.rendition_sha256
        ):
            raise PilotError(
                f"{artwork_id}: refusing to overwrite an unrelated or changed resource"
            )
        api.call("add_resource_to_collection", resource=ref, collection=collection)
        info = api.call("get_resource_data", resource=ref)
        if not isinstance(info, dict):
            raise PilotError("ResourceSpace returned invalid resource information")
        if not info.get("file_extension"):
            api.call("upload_file", ref=ref, no_exif=True, autorotate=False, upload=item.rendition)
        elif str(info["file_extension"]).lower() not in {"jpg", "jpeg"}:
            raise PilotError(f"{artwork_id}: existing resource is not a JPEG; import stopped")
        for name, value in values.items():
            if name in fields:
                api.call("update_field", resource=ref, field=fields[name], value=value)
        imported.append({"id": artwork_id, "resource_ref": ref})
    final = result_rows(
        api.call("do_search", search=f"!collection{collection}", fetchrows="0,21", archive="0")
    )
    if len(final) != PILOT_SIZE:
        raise PilotError("Import did not produce exactly 20 pilot resources")
    for item in manifest.items:
        checked_hash(item.original, item.original_sha256, "original")
        checked_hash(item.rendition, item.rendition_sha256, "rendition")
    return {
        "status": "imported",
        "collection_id": collection,
        "item_count": len(imported),
        "items": imported,
        "originals_unchanged": True,
        "storage_behavior": "managed TV rendition copies; read-only original provenance links",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--originals-root", required=True, type=Path)
    parser.add_argument("--renditions-root", required=True, type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="Validate only (the default)")
    action.add_argument("--apply", action="store_true", help="Import into the dedicated pilot")
    parser.add_argument("--url", default=os.environ.get("RESOURCESPACE_PILOT_URL", ""))
    parser.add_argument("--user", default=os.environ.get("RESOURCESPACE_PILOT_USER", ""))
    args = parser.parse_args(argv)
    api: AdminAPI | None = None
    try:
        manifest = load_manifest(args.manifest, args.originals_root, args.renditions_root)
        if args.apply:
            api = AdminAPI(args.url, args.user, os.environ.get("RESOURCESPACE_PILOT_API_KEY", ""))
            report = import_pilot(manifest, api)
        else:
            report = {
                "status": "validated",
                "dry_run": True,
                "network_requests": 0,
                "item_count": len(manifest.items),
                "collection_name": manifest.collection_name,
                "originals_unchanged": True,
                "items": [
                    {"id": item.metadata["id"], "title": item.metadata["title"]}
                    for item in manifest.items
                ],
            }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (PilotError, OSError, json.JSONDecodeError) as exc:
        print(f"Pilot stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        if api is not None:
            api.close()


if __name__ == "__main__":
    raise SystemExit(main())
