"""Durable dislikes and genuinely new, free public-domain replacements.

No ResourceSpace/NAS writes, provider charges or TV commands. Rejected works
stay excluded after refresh; prepared replacements survive worker restarts.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx2 as httpx
from PIL import Image, ImageOps

from frameart import public_domain
from frameart.art_catalog import ArtCatalogStore, ArtistMetadata, ArtworkMetadata, _webp_thumbnail
from frameart.art_profile import ArtProfileStore
from frameart.daily_art import DailyArtStore

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_IMAGE_URL = re.compile(r"^https://www\.artic\.edu/iiif/2/[A-Za-z0-9-]+/full/1686,/0/default\.jpg$")
_MAX_IMAGE = 32 * 1024 * 1024
_RETRY_DETAIL = "A new eligible artwork could not be prepared. Automatic retry is scheduled."


def _fingerprints(record: ArtworkMetadata) -> set[str]:
    title = re.sub(r"\W+", " ", record.title.casefold()).strip()
    artist = re.sub(r"\W+", " ", record.artist.name.casefold()).strip()
    result = {"title:" + title + ":" + artist}
    if record.source_url:
        parsed = urlsplit(record.source_url)
        path = parsed.path.rstrip("/")
        # A museum's optional human-readable slug is not a different artwork.
        if parsed.hostname in {"www.artic.edu", "artic.edu"}:
            match = re.match(r"/artworks/(\d+)", path)
            if match:
                result.add("aic:" + match[1])
        result.add("url:" + (parsed.hostname or "").lower() + path)
    return result


class CuratedReplacementStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.catalog = ArtCatalogStore(data_dir)

    @contextmanager
    def _db(self):
        connection = self.catalog._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _used(db) -> set[str]:
        result = set()
        for row in db.execute(
            "SELECT metadata FROM art_catalog_items UNION ALL "
            "SELECT metadata FROM curated_local_art UNION ALL "
            "SELECT metadata FROM curated_rejections"
        ):
            result.update(_fingerprints(ArtworkMetadata.model_validate_json(row[0])))
        return result

    def used(self) -> set[str]:
        with self._db() as db:
            return self._used(db)

    def summary(self) -> dict:
        with self._db() as db:
            count = db.execute("SELECT count(*) FROM curated_rejections").fetchone()[0]
            pending = db.execute(
                "SELECT count(*) FROM curated_replacement_jobs WHERE status!='completed'"
            ).fetchone()[0]
            rows = db.execute(
                "SELECT j.artwork_id,j.replacement_id,j.status,j.attempts,j.retry_at,j.detail,"
                "r.metadata FROM curated_replacement_jobs j JOIN curated_rejections r "
                "ON r.artwork_id=j.artwork_id "
                "ORDER BY (j.status='completed'),j.updated_at DESC LIMIT 50"
            ).fetchall()
        jobs = []
        for row in rows:
            job = dict(row)
            job["title"] = json.loads(job.pop("metadata"))["title"]
            jobs.append(job)
        return {"rejected_count": count, "pending_count": pending, "jobs": jobs}

    def dislike(self, artwork_id: str) -> dict:
        if not _ID.fullmatch(artwork_id):
            raise ValueError("Invalid artwork ID.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT artwork_id,status,replacement_id FROM curated_replacement_jobs "
                "WHERE artwork_id=?",
                (artwork_id,),
            ).fetchone()
            if previous:
                return dict(previous)
            record = db.execute(
                "SELECT metadata FROM art_catalog_items WHERE id=? UNION ALL "
                "SELECT metadata FROM curated_local_art WHERE id=? AND ready=1",
                (artwork_id, artwork_id),
            ).fetchone()
            if record is None:
                raise KeyError("Artwork is not in the active collection.")
            if (
                db.execute(
                    "SELECT count(*) FROM curated_replacement_jobs WHERE status!='completed'"
                ).fetchone()[0]
                >= 1000
            ):
                raise ValueError("The replacement queue is full. Try again later.")
            now = time.time()
            db.execute("INSERT INTO curated_rejections VALUES(?,?,?)", (artwork_id, record[0], now))
            db.execute(
                "INSERT INTO curated_replacement_jobs(artwork_id,queued_at,updated_at) "
                "VALUES(?,?,?)",
                (artwork_id, now, now),
            )
            # Remove cached pixels/metadata, not the protected NAS master or
            # upstream collection. The tiny rejection record prevents re-import.
            db.execute("DELETE FROM art_catalog_items WHERE id=?", (artwork_id,))
            db.execute("DELETE FROM curated_local_art WHERE id=?", (artwork_id,))
        return {"artwork_id": artwork_id, "status": "queued", "replacement_id": None}

    def claim(self) -> dict | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            # ASGI may have several processes. Only one household worker may
            # contact museums at once; a dead process's lease expires safely.
            if db.execute(
                "SELECT 1 FROM curated_replacement_jobs WHERE status='running' AND lease_until>?",
                (now,),
            ).fetchone():
                return None
            row = db.execute(
                "SELECT * FROM curated_replacement_jobs WHERE status!='completed' "
                "AND retry_at<=? AND lease_until<=? ORDER BY queued_at LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE curated_replacement_jobs SET status='running',claim_token=?,lease_until=?,"
                "attempts=attempts+1,updated_at=? WHERE artwork_id=?",
                (token, now + 300, now, row["artwork_id"]),
            )
            metadata = db.execute(
                "SELECT metadata FROM curated_rejections WHERE artwork_id=?", (row["artwork_id"],)
            ).fetchone()[0]
        return {**dict(row), "claim_token": token, "metadata": metadata}

    def retry(self, artwork_id: str) -> None:
        with self._db() as db:
            changed = db.execute(
                "UPDATE curated_replacement_jobs SET retry_at=0 WHERE artwork_id=? "
                "AND status='retrying'",
                (artwork_id,),
            ).rowcount
        if not changed:
            raise ValueError("This replacement is not waiting for a retry.")

    def prepare(self, job: dict, record: ArtworkMetadata, source: bytes, rendition: bytes) -> None:
        with Image.open(io.BytesIO(rendition)) as image:
            if image.format != "JPEG" or image.mode != "RGB" or image.size != (3840, 2160):
                raise ValueError("Replacement must be a full-frame 3840×2160 RGB JPEG.")
            image.load()
        if max(len(source), len(rendition)) > _MAX_IMAGE:
            raise ValueError("Replacement image is too large.")
        digest = hashlib.sha256(rendition).hexdigest()
        source_hash = hashlib.sha256(source).hexdigest()
        preview = _webp_thumbnail(rendition)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT claim_token,replacement_id FROM curated_replacement_jobs "
                "WHERE artwork_id=?",
                (job["artwork_id"],),
            ).fetchone()
            if current["claim_token"] != job["claim_token"] or current["replacement_id"]:
                raise ValueError("Replacement claim is no longer current.")
            if db.execute(
                "SELECT 1 FROM art_catalog_items WHERE id=? UNION ALL "
                "SELECT 1 FROM curated_rejections WHERE artwork_id=? LIMIT 1",
                (record.id, record.id),
            ).fetchone():
                raise ValueError("This artwork ID has already been used.")
            if _fingerprints(record).intersection(self._used(db)):
                raise ValueError("This artwork was already collected or rejected.")
            identity = next(
                (value for value in _fingerprints(record) if value.startswith("aic:")),
                "id:" + record.id,
            )
            db.execute(
                "INSERT INTO curated_seen_sources VALUES(?,?,?)", (identity, source_hash, digest)
            )
            db.execute(
                "INSERT INTO curated_local_art(id,metadata,thumbnail,rendition,source_image,"
                "rendition_hash) VALUES(?,?,?,?,?,?)",
                (record.id, record.model_dump_json(), preview, rendition, source, digest),
            )
            db.execute(
                "UPDATE curated_replacement_jobs SET replacement_id=?,updated_at=? "
                "WHERE artwork_id=?",
                (record.id, time.time(), job["artwork_id"]),
            )

    def prepared(self, job: dict) -> ArtworkMetadata | None:
        with self._db() as db:
            row = db.execute(
                "SELECT a.metadata FROM curated_local_art a JOIN curated_replacement_jobs j "
                "ON j.replacement_id=a.id WHERE j.artwork_id=?",
                (job["artwork_id"],),
            ).fetchone()
        return ArtworkMetadata.model_validate_json(row[0]) if row else None

    def finish(self, job: dict, replacement_id: str) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE curated_replacement_jobs SET status='completed',claim_token=NULL,"
                "lease_until=0,retry_at=0,detail='',updated_at=? "
                "WHERE artwork_id=? AND claim_token=?",
                (time.time(), job["artwork_id"], job["claim_token"]),
            ).rowcount
            if changed:
                db.execute("UPDATE curated_local_art SET ready=1 WHERE id=?", (replacement_id,))

    def failed(self, job: dict) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE curated_replacement_jobs SET status='retrying',lease_until=0,"
                "claim_token=NULL,"
                "retry_at=?,updated_at=?,detail=? WHERE artwork_id=? AND claim_token=?",
                (
                    time.time() + min(3600, 60 * 2 ** min(job["attempts"], 6)),
                    time.time(),
                    _RETRY_DETAIL,
                    job["artwork_id"],
                    job["claim_token"],
                ),
            )


def _queries(profile: dict | None, old: ArtworkMetadata) -> list[str]:
    mapping = {
        "abstract": "abstract painting",
        "geometric": "geometric painting",
        "modern": "modern landscape",
        "bold": "colorful painting",
        "impression": "impressionist landscape",
        "watercolor": "watercolor landscape",
        "coast": "seascape",
        "landscape": "landscape",
        "nature": "landscape",
    }
    tags = [
        item.get("tag", "")
        for item in (profile or {}).get("derived", {}).get("preferred_tags", [])[:8]
    ]
    queries = [query for tag in tags for key, query in mapping.items() if key in tag]
    for season in ("winter", "spring", "summer", "autumn"):
        if any(season in tag.lower() for tag in old.tags):
            queries.insert(0, season + " landscape")
    return list(dict.fromkeys([*queries, "landscape", "abstract painting", "painting"]))[:4]


def find_replacement(settings, old: ArtworkMetadata, used: set[str], *, attempt=0):
    """Bounded public-domain search; never invoke a paid image provider."""
    profile = ArtProfileStore(settings.data_dir).get_profile()
    queries = _queries(profile, old)
    offset = attempt % len(queries)
    queries = queries[offset:] + queries[:offset]
    downloads = 0
    last_request = 0.0
    deadline = time.monotonic() + 200

    def metadata_call(function, *args, **kwargs):
        nonlocal last_request
        if time.monotonic() > deadline:
            raise TimeoutError("The bounded museum search timed out.")
        delay = 1 - (time.monotonic() - last_request)
        if delay > 0:
            time.sleep(delay)
        last_request = time.monotonic()
        return function(*args, **kwargs)

    for query in queries:
        # Reuse the existing museum adapter; recheck rights before downloading.
        candidates = metadata_call(public_domain.search_artworks, "aic", query, limit=24)
        if candidates:
            offset = (attempt * 4) % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        for candidate in candidates:
            identity = str(candidate.get("artwork_id", ""))
            if not identity.isdigit() or "aic:" + identity in used:
                continue
            if downloads >= 4:
                raise ValueError("No eligible replacement in this bounded search; retry later.")
            downloads += 1
            try:
                item = metadata_call(public_domain.get_artwork, "aic", identity)
            except (httpx.HTTPError, ValueError):
                continue
            if item.get("is_public_domain") is not True or not _IMAGE_URL.fullmatch(
                item.get("image_url", "")
            ):
                continue
            artist = item.get("artist") or "Unknown artist"
            record = ArtworkMetadata(
                id="curated-aic-" + identity,
                title=item["title"],
                artist=ArtistMetadata(
                    id="artist-" + hashlib.sha256(artist.encode()).hexdigest()[:20], name=artist
                ),
                date=item.get("date"),
                institution="Art Institute of Chicago",
                source_url=item["source_url"],
                license="Public Domain (CC0)",
                license_url="https://creativecommons.org/publicdomain/zero/1.0/",
                tags=["full-frame", "curated-replacement", query],
                width=3840,
                height=2160,
                crop_notes=(
                    "Center-cropped and scaled to edge-to-edge 3840×2160; no padding or matte."
                ),
            )
            if _fingerprints(record).intersection(used):
                continue
            try:
                with (
                    httpx.Client(
                        timeout=20,
                        follow_redirects=False,
                        headers={
                            "User-Agent": "FrameArt/0.2",
                            "Referer": "https://www.artic.edu/",
                            "Accept": "*/*",
                        },
                    ) as client,
                    client.stream("GET", item["image_url"]) as response,
                ):
                    response.raise_for_status()
                    source = bytearray()
                    for chunk in response.iter_bytes(64 * 1024):
                        if time.monotonic() > deadline:
                            raise TimeoutError("The bounded image download timed out.")
                        if len(source) + len(chunk) > _MAX_IMAGE:
                            raise ValueError("Museum image exceeds the download limit.")
                        source.extend(chunk)
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(source)) as image:
                        if image.width * image.height > 50_000_000:
                            continue
                        image = ImageOps.exif_transpose(image)
                        if (
                            image.width < 1200
                            or image.height < 650
                            or not (1.4 <= image.width / image.height <= 2.3)
                        ):
                            continue
                        full_frame = ImageOps.fit(
                            image.convert("RGB"), (3840, 2160), method=Image.Resampling.LANCZOS
                        )
                        output = io.BytesIO()
                        full_frame.save(output, format="JPEG", quality=94, subsampling=0)
                return record, bytes(source), output.getvalue()
            except (
                httpx.HTTPError,
                ValueError,
                OSError,
                Image.DecompressionBombWarning,
                Image.DecompressionBombError,
            ):
                continue
    raise ValueError("No new eligible public-domain artwork was found.")


class CuratedReplacementService:
    def __init__(self, settings_loader, finder=find_replacement):
        self.settings_loader = settings_loader
        self.finder = finder
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def dislike(self, artwork_id: str) -> dict:
        result = CuratedReplacementStore(self.settings_loader().data_dir).dislike(artwork_id)
        self._wake.set()
        return result

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="curated-replacements", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self.run_once():
                    self._stop.wait(1)  # One worker; throttle automatic museum searches.
                    continue
            except Exception:
                pass  # Only fixed, safe error text is persisted; no provider response logs.
            self._wake.wait(15)
            self._wake.clear()

    def run_once(self) -> bool:
        settings = self.settings_loader()
        store = CuratedReplacementStore(settings.data_dir)
        job = store.claim()
        if job is None:
            return False
        try:
            from frameart.art_delivery import purge_cached_artwork

            purge_cached_artwork(settings.data_dir, job["artwork_id"])
            replacement = store.prepared(job)
            if replacement is None:
                old = ArtworkMetadata.model_validate_json(job["metadata"])
                replacement, source, rendition = self.finder(
                    settings, old, store.used(), attempt=job["attempts"]
                )
                store.prepare(job, replacement, source, rendition)
            # Idempotent across the separate calendar DB; prepared pixels stay
            # durable but invisible until every unplayed slot is redirected.
            DailyArtStore(settings.data_dir).replace_unattempted_artwork(
                job["artwork_id"], replacement.id
            )
            purge_cached_artwork(settings.data_dir, job["artwork_id"])
            store.finish(job, replacement.id)
        except Exception:
            store.failed(job)
        return True
