"""Bounded full-frame rendition caching and guarded catalog delivery.

The NAS is never mounted or written by this module. Full-resolution bytes are
read through the catalog's read-only ResourceSpace client. TV content mappings
are checked against the actual inventory on every delivery (including resets).
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

from PIL import Image

from frameart.art_catalog import ArtCatalogStore, integration_configured
from frameart.automation import AutomationStore
from frameart.library import LibraryStore
from frameart.resourcespace import ResourceSpaceClient, ResourceSpaceError

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_locks_guard = threading.Lock()
_tv_locks: dict[str, threading.Lock] = {}
_cache_lock = threading.RLock()


def tv_delivery_lock(ip: str) -> threading.Lock:
    """Shared whole-delivery arbitration for album and catalog operations.

    The album records priority before waiting here. An in-flight daily operation
    finishes or defers first, and the higher-priority album then has the last word.
    """
    with _locks_guard:
        return _tv_locks.setdefault(ip, threading.Lock())


_MAX_TV_THUMBNAIL_BYTES = 8 * 1024**2


def _pairing_token_hash(profile) -> str | None:
    """Hash one bounded regular token file without following symbolic links.

    These hashes are private local identity evidence, never API response data.
    An unknown or changing pairing epoch is not permission to reuse/delete art.
    """
    path = getattr(profile, "token_file", None)
    if not path:
        # Match the controller's existing-file discovery, without using its
        # resolver (which creates directories as part of pairing).
        from frameart.tv.controller import _find_token_file

        try:
            ip = getattr(profile, "ip", None)
            if not isinstance(ip, str):
                return None
            ipaddress.ip_address(ip)
            path = _find_token_file(ip)
        except (OSError, TypeError, ValueError):
            return None
        if not path:
            return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= 4096:
                return None
            payload = source.read(4097)
            after = os.fstat(source.fileno())
        if (
            not payload.strip()
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            return None
        return hashlib.sha256(payload).hexdigest()
    except (OSError, TypeError, ValueError):
        return None


def resolve_targets(settings, target_type: str, target_id: str) -> list[str]:
    """Resolve the saved profile IDs, never arbitrary browser-supplied IPs."""
    if target_type == "tv":
        targets = [target_id]
    elif target_type == "group":
        group = AutomationStore(settings.data_dir).get_group(target_id)
        if group is None:
            raise ValueError("The selected TV group no longer exists.")
        targets = group["tv_profile_ids"]
    else:
        raise ValueError("Choose an individual TV or TV group.")
    targets = list(dict.fromkeys(targets))
    if not targets or any(target not in settings.tvs for target in targets):
        raise ValueError("The selected target contains a missing TV profile.")
    return targets


@contextmanager
def _database(data_dir: Path):
    connection = sqlite3.connect(Path(data_dir) / "frameart.sqlite3", timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class CatalogDeliveryStore:
    """Durable requests, per-device content IDs and small local image cache."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _database(self.data_dir) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_delivery_jobs (
                  id TEXT PRIMARY KEY, artwork_id TEXT NOT NULL,
                  targets TEXT NOT NULL, status TEXT NOT NULL,
                  results TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_tv_content (
                  tv_ip TEXT NOT NULL, rendition_hash TEXT NOT NULL,
                  content_id TEXT NOT NULL, artwork_id TEXT NOT NULL,
                  PRIMARY KEY(tv_ip, rendition_hash)
                );
                CREATE TABLE IF NOT EXISTS catalog_renditions (
                  artwork_id TEXT PRIMARY KEY, resource_ref INTEGER NOT NULL,
                  content_hash TEXT NOT NULL, accessed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_rendition_sources (
                  artwork_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_daily_owned (
                  tv_ip TEXT NOT NULL, content_id TEXT NOT NULL,
                  profile_id TEXT NOT NULL, used_at REAL NOT NULL,
                  PRIMARY KEY(tv_ip,content_id)
                );
                CREATE TABLE IF NOT EXISTS catalog_tv_fingerprints (
                  tv_ip TEXT NOT NULL, content_id TEXT NOT NULL,
                  thumbnail_hash TEXT NOT NULL, token_hash TEXT NOT NULL,
                  PRIMARY KEY(tv_ip,content_id)
                );
                """
            )
        os.chmod(self.data_dir / "frameart.sqlite3", 0o600)

    def enqueue(self, artwork_id: str, targets: list[str]) -> dict:
        identity = uuid.uuid4().hex
        with _database(self.data_dir) as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT COUNT(*) FROM catalog_delivery_jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if count >= 20:
                raise ValueError(
                    "The delivery queue is full. Wait for existing requests to finish."
                )
            db.execute(
                "INSERT INTO catalog_delivery_jobs "
                "(id,artwork_id,targets,status,created_at) VALUES(?,?,?,'queued',?)",
                (identity, artwork_id, json.dumps(targets), time.time()),
            )
        return self.get(identity)

    def get(self, job_id: str) -> dict | None:
        with _database(self.data_dir) as db:
            row = db.execute("SELECT * FROM catalog_delivery_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["targets"] = json.loads(result["targets"])
        result["results"] = json.loads(result["results"])
        return result

    def recover(self) -> None:
        # An interrupted mutation may already have reached a TV. Never replay
        # it just because the service restarted; the user can reconcile/send.
        with _database(self.data_dir) as db:
            db.execute(
                "UPDATE catalog_delivery_jobs SET status='completed', results=? "
                "WHERE status='running'",
                (
                    json.dumps(
                        [
                            {
                                "tv": None,
                                "status": "uncertain",
                                "detail": "Interrupted delivery was not automatically repeated.",
                            }
                        ]
                    ),
                ),
            )

    def claim(self) -> dict | None:
        with _database(self.data_dir) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM catalog_delivery_jobs WHERE status='queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE catalog_delivery_jobs SET status='running' WHERE id=?", (row[0],))
        return self.get(row[0])

    def finish(self, job_id: str, results: list[dict]) -> None:
        with _database(self.data_dir) as db:
            db.execute(
                "UPDATE catalog_delivery_jobs SET status='completed',results=? WHERE id=?",
                (json.dumps(results), job_id),
            )
            # Keep a bounded recent audit trail; never prune queued/running work.
            db.execute(
                "DELETE FROM catalog_delivery_jobs WHERE status='completed' AND id NOT IN "
                "(SELECT id FROM catalog_delivery_jobs ORDER BY created_at DESC LIMIT 200)"
            )


class CatalogDeliveryService:
    """One manual-delivery worker and a reusable guarded daily-art callback."""

    def __init__(self, settings_loader) -> None:
        self.settings_loader = settings_loader
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def enqueue(self, artwork_id: str, target_type: str, target_id: str) -> dict:
        settings = self.settings_loader()
        if (
            not _ID.fullmatch(artwork_id)
            or ArtCatalogStore(settings.data_dir).get(artwork_id) is None
        ):
            raise ValueError("The artwork is not in the local catalog.")
        targets = resolve_targets(settings, target_type, target_id)
        job = CatalogDeliveryStore(settings.data_dir).enqueue(artwork_id, targets)
        self._wake.set()
        return job

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        CatalogDeliveryStore(self.settings_loader().data_dir).recover()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="catalog-delivery")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = None
            store = None
            try:
                store = CatalogDeliveryStore(self.settings_loader().data_dir)
                job = store.claim()
                if job:
                    if time.time() - job["created_at"] > 3600:
                        results = [
                            {
                                "tv": tv,
                                "status": "error",
                                "detail": "Request expired without changing the TV.",
                            }
                            for tv in job["targets"]
                        ]
                    else:
                        results = self._deliver_targets(
                            job["artwork_id"], job["targets"], manual=True
                        )
                    store.finish(job["id"], results)
                    continue
            except Exception:
                # Errors are persisted as safe per-TV results by delivery; no
                # raw upstream URLs, tokens or household content are logged.
                if job is not None and store is not None:
                    # Startup recovery retains any claim if even this local
                    # error receipt cannot be written.
                    with suppress(Exception):
                        store.finish(
                            job["id"],
                            [
                                {
                                    "tv": tv,
                                    "status": "uncertain",
                                    "detail": "Delivery stopped unexpectedly; it was not repeated.",
                                }
                                for tv in job["targets"]
                            ],
                        )
            self._wake.wait(2)
            self._wake.clear()

    def deliver(
        self, artwork_id: str, target_type: str, target_id: str, day_key: str, *, skip_tvs: set[str]
    ) -> dict:
        settings = self.settings_loader()
        targets = [
            tv for tv in resolve_targets(settings, target_type, target_id) if tv not in skip_tvs
        ]
        return {
            "results": self._deliver_targets(
                artwork_id,
                targets,
                manual=False,
                day_key=day_key,
            )
        }

    def reconcile(
        self,
        artwork_id: str,
        target_type: str,
        target_id: str,
        day_key: str,
        tv_id: str,
    ) -> str:
        """Read-only TV evidence before retrying an uncertain daily selection.

        Unknown uploads without a verified mapping are never blindly repeated.
        Local cache/history bookkeeping is allowed; this sends no TV mutation.
        """
        from frameart.tv.controller import _get_current_artwork, get_status

        settings = self.settings_loader()
        if tv_id not in resolve_targets(settings, target_type, target_id):
            return "uncertain"
        profile = settings.tvs[tv_id]
        lock = tv_delivery_lock(profile.ip)
        if not lock.acquire(blocking=False):
            return "uncertain"
        try:
            state = get_status(profile)
            if not (state.reachable and state.art_mode_supported and state.art_mode_on):
                return "uncertain"
            artwork = ArtCatalogStore(settings.data_dir).get(artwork_id)
            if artwork is None:
                return "uncertain"
            _payload, digest = self._rendition(settings, artwork)
            with _database(settings.data_dir) as db:
                row = db.execute(
                    "SELECT content_id FROM catalog_tv_content WHERE tv_ip=? AND rendition_hash=?",
                    (profile.ip, digest),
                ).fetchone()
            if not row or not self._fingerprint_matches(settings, profile, row[0]):
                return "uncertain"
            current = _get_current_artwork(profile, timeout_sec=5)
            if current == row[0]:
                LibraryStore(settings.data_dir).record_display(
                    job_id=None,
                    content_id=row[0],
                    tv_target=tv_id,
                    source="daily-art",
                )
                return "displayed"
            # A nonempty status/error string is not proof of a different
            # selected artwork. Unknown identifiers must not authorize retries.
            known_current = isinstance(current, str) and re.fullmatch(
                r"(?:MY|SAM)[_-][A-Za-z0-9._-]{1,120}",
                current,
            )
            return "retryable" if known_current else "uncertain"
        except Exception:
            return "uncertain"
        finally:
            lock.release()

    def _rendition(self, settings, artwork) -> tuple[bytes, str]:
        if not artwork.rendition_ref or not integration_configured(settings):
            raise ResourceSpaceError("The artwork rendition is not configured.")
        directory = Path(settings.data_dir) / "catalog-renditions"
        with _cache_lock:
            if directory.is_symlink():
                raise ResourceSpaceError("The local rendition cache is not a regular directory.")
            directory.mkdir(mode=0o700, exist_ok=True)
            with _database(settings.data_dir) as db:
                state = db.execute(
                    "SELECT value FROM art_catalog_state WHERE key='last_synced_at'"
                ).fetchone()
                source_hash = hashlib.sha256(
                    json.dumps(
                        [
                            settings.resourcespace_url,
                            settings.resourcespace_user,
                            settings.resourcespace_collection,
                            artwork.model_dump_json(),
                            state[0] if state else None,
                        ]
                    ).encode()
                ).hexdigest()
                row = db.execute(
                    "SELECT content_hash FROM catalog_renditions "
                    "WHERE artwork_id=? AND resource_ref=?",
                    (artwork.id, artwork.rendition_ref),
                ).fetchone()
                source = db.execute(
                    "SELECT source_hash FROM catalog_rendition_sources WHERE artwork_id=?",
                    (artwork.id,),
                ).fetchone()
            if row and source and source[0] == source_hash and _HASH.fullmatch(row[0]):
                path = directory / (row[0] + ".jpg")
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= 32 * 1024**2:
                    payload = path.read_bytes()
                    if hashlib.sha256(payload).hexdigest() == row[0]:
                        with _database(settings.data_dir) as db:
                            db.execute(
                                "UPDATE catalog_renditions SET accessed_at=? WHERE artwork_id=?",
                                (time.time(), artwork.id),
                            )
                        return payload, row[0]
            client = ResourceSpaceClient(
                settings.resourcespace_url,
                settings.resourcespace_user,
                settings.resourcespace_api_key.get_secret_value(),
                timeout=settings.resourcespace_timeout,
            )
            payload = client.rendition(artwork.rendition_ref)
            if len(payload) > 32 * 1024**2:
                raise ResourceSpaceError("The artwork rendition is too large.")
            with Image.open(io.BytesIO(payload)) as image:
                if image.format != "JPEG" or image.size != (3840, 2160) or image.mode != "RGB":
                    raise ResourceSpaceError(
                        "Only a full-frame 3840×2160 RGB JPEG TV rendition can be sent."
                    )
                image.load()
            digest = hashlib.sha256(payload).hexdigest()
            path = directory / (digest + ".jpg")
            if path.is_symlink():
                raise ResourceSpaceError("The local rendition cache contains an unsafe path.")
            temporary = directory / (digest + "." + uuid.uuid4().hex + ".tmp")
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as destination:
                    destination.write(payload)
                    destination.flush()
                    os.fsync(destination.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            with _database(settings.data_dir) as db:
                db.execute(
                    "INSERT OR REPLACE INTO catalog_renditions VALUES(?,?,?,?)",
                    (artwork.id, artwork.rendition_ref, digest, time.time()),
                )
                db.execute(
                    "INSERT OR REPLACE INTO catalog_rendition_sources VALUES(?,?)",
                    (artwork.id, source_hash),
                )
                old = db.execute(
                    "SELECT artwork_id,content_hash FROM catalog_renditions "
                    "ORDER BY accessed_at DESC LIMIT -1 OFFSET 8"
                ).fetchall()
                for item in old:
                    db.execute("DELETE FROM catalog_renditions WHERE artwork_id=?", (item[0],))
                    db.execute(
                        "DELETE FROM catalog_rendition_sources WHERE artwork_id=?", (item[0],)
                    )
                live = {
                    item[0] for item in db.execute("SELECT content_hash FROM catalog_renditions")
                }
            obsolete_hashes = {item[1] for item in old} | ({row[0]} if row else set())
            for old_hash in obsolete_hashes:
                if _HASH.fullmatch(old_hash) and old_hash not in live:
                    obsolete = directory / (old_hash + ".jpg")
                    if obsolete.is_file() and not obsolete.is_symlink():
                        obsolete.unlink()
            return payload, digest

    @staticmethod
    def _fingerprint(profile, content_id: str) -> tuple[str, str] | None:
        """Read fresh TV evidence; cached HTTP thumbnails must not be used here."""
        from frameart.tv.controller import get_art_thumbnail

        token_hash = _pairing_token_hash(profile)
        if token_hash is None:
            return None
        try:
            payload = get_art_thumbnail(profile, content_id)
        except Exception:
            return None
        if (
            not isinstance(payload, (bytes, bytearray))
            or not 1 <= len(payload) <= _MAX_TV_THUMBNAIL_BYTES
            or _pairing_token_hash(profile) != token_hash
        ):
            return None
        return hashlib.sha256(payload).hexdigest(), token_hash

    @staticmethod
    def _forget_content(settings, profile, content_id: str) -> None:
        """Forget only local claims, preserving an unverified TV file untouched."""
        with _database(settings.data_dir) as db:
            db.execute(
                "DELETE FROM catalog_tv_content WHERE tv_ip=? AND content_id=?",
                (profile.ip, content_id),
            )
            db.execute(
                "DELETE FROM catalog_daily_owned WHERE tv_ip=? AND content_id=?",
                (profile.ip, content_id),
            )
            db.execute(
                "DELETE FROM catalog_tv_fingerprints WHERE tv_ip=? AND content_id=?",
                (profile.ip, content_id),
            )

    @staticmethod
    def _forget_obsolete_epoch(settings, profile, token_hash: str) -> None:
        """A new pairing epoch invalidates local claims, never remote files."""
        with _database(settings.data_dir) as db:
            for table in ("catalog_tv_content", "catalog_daily_owned"):
                db.execute(
                    f"DELETE FROM {table} WHERE tv_ip=? AND content_id NOT IN "
                    "(SELECT content_id FROM catalog_tv_fingerprints "
                    "WHERE tv_ip=? AND token_hash=?)",
                    (profile.ip, profile.ip, token_hash),
                )
            db.execute(
                "DELETE FROM catalog_tv_fingerprints WHERE tv_ip=? AND token_hash!=?",
                (profile.ip, token_hash),
            )

    @classmethod
    def _record_fingerprint(cls, settings, profile, content_id: str) -> bool:
        fingerprint = cls._fingerprint(profile, content_id)
        if fingerprint is None:
            cls._forget_content(settings, profile, content_id)
            return False
        with _database(settings.data_dir) as db:
            db.execute(
                "INSERT OR REPLACE INTO catalog_tv_fingerprints "
                "(tv_ip,content_id,thumbnail_hash,token_hash) VALUES(?,?,?,?)",
                (profile.ip, content_id, *fingerprint),
            )
        cls._forget_obsolete_epoch(settings, profile, fingerprint[1])
        return True

    @classmethod
    def _fingerprint_matches(cls, settings, profile, content_id: str) -> bool:
        with _database(settings.data_dir) as db:
            stored = db.execute(
                "SELECT thumbnail_hash,token_hash FROM catalog_tv_fingerprints "
                "WHERE tv_ip=? AND content_id=?",
                (profile.ip, content_id),
            ).fetchone()
        current_token = _pairing_token_hash(profile)
        if stored is not None and current_token and stored[1] != current_token:
            cls._forget_obsolete_epoch(settings, profile, current_token)
        if (
            stored is None
            or current_token is None
            or not _HASH.fullmatch(stored[0])
            or stored[1] != current_token
        ):
            cls._forget_content(settings, profile, content_id)
            return False
        current = cls._fingerprint(profile, content_id)
        if current != (stored[0], stored[1]):
            cls._forget_content(settings, profile, content_id)
            return False
        return True

    def _deliver_targets(
        self,
        artwork_id: str,
        targets: list[str],
        *,
        manual: bool,
        day_key: str | None = None,
    ) -> list[dict]:
        settings = self.settings_loader()
        CatalogDeliveryStore(settings.data_dir)
        artwork = ArtCatalogStore(settings.data_dir).get(artwork_id)
        if artwork is None:
            return [
                {"tv": tv, "status": "error", "detail": "Artwork is no longer in the catalog."}
                for tv in targets
            ]
        results = []
        rendition = None
        for tv in targets:
            profile = settings.tvs.get(tv)
            if profile is None:
                results.append({"tv": tv, "status": "error", "detail": "TV profile is missing."})
                continue
            lock = tv_delivery_lock(profile.ip)
            if not lock.acquire(blocking=False):
                results.append(
                    {"tv": tv, "status": "deferred", "detail": "Another delivery is running."}
                )
                continue
            mutation_started = False
            try:
                from frameart.daily_art import DailyArtStore
                from frameart.tv.controller import (
                    get_status,
                    list_art_deduplicated,
                    switch_art,
                    upload_image,
                )

                def held(identity=tv, address=profile.ip) -> bool:
                    state = DailyArtStore(settings.data_dir).snapshot()
                    if day_key is not None and (
                        not state["enabled"]
                        or state["today"]["date"] != day_key
                        or state["today"]["artwork_id"] != artwork_id
                        or state["today"]["status"] == "held"
                        or identity
                        not in resolve_targets(
                            settings,
                            state["target_type"],
                            state["target_id"],
                        )
                    ):
                        return True
                    if day_key is not None:
                        from frameart.live_score import LiveScoreStore

                        # A sports feed can be enabled after Daily Art was
                        # configured. Recheck at dispatch, not only on save.
                        for feed in LiveScoreStore(settings.data_dir).list_trackers():
                            if feed["enabled"] and identity in resolve_targets(
                                settings,
                                feed["target_type"],
                                feed["target_id"],
                            ):
                                return True
                    return any(item["tv"] in {identity, address} for item in state.get("holds", []))

                if not manual and held():
                    results.append(
                        {"tv": tv, "status": "deferred", "detail": "Manual choice is held today."}
                    )
                    continue
                status = get_status(profile)
                if not (status.reachable and status.art_mode_supported and status.art_mode_on):
                    results.append(
                        {
                            "tv": tv,
                            "status": "deferred",
                            "detail": "TV is busy, offline or not confirmed in Art Mode.",
                        }
                    )
                    continue
                if rendition is None:
                    rendition = self._rendition(settings, artwork)
                payload, digest = rendition
                inventory = {
                    str(item.get("content_id", "")) for item in list_art_deduplicated(profile)
                }
                with _database(settings.data_dir) as db:
                    row = db.execute(
                        "SELECT content_id FROM catalog_tv_content "
                        "WHERE tv_ip=? AND rendition_hash=?",
                        (profile.ip, digest),
                    ).fetchone()
                content_id = row[0] if row and row[0] in inventory else None
                if content_id and not self._fingerprint_matches(settings, profile, content_id):
                    content_id = None
                if row and content_id is None:
                    self._forget_content(settings, profile, row[0])
                if not content_id:
                    if not manual and held():
                        results.append(
                            {
                                "tv": tv,
                                "status": "deferred",
                                "detail": "Daily Art was paused before upload.",
                            }
                        )
                        continue
                    mutation_started = True
                    uploaded = upload_image(
                        profile, payload, file_type="JPEG", matte="none", require_art_mode=True
                    )
                    if uploaded.deferred:
                        mutation_started = False
                        results.append(
                            {
                                "tv": tv,
                                "status": "deferred",
                                "detail": "TV left Art Mode; nothing was uploaded.",
                            }
                        )
                        continue
                    if not uploaded.success or not uploaded.content_id:
                        results.append(
                            {
                                "tv": tv,
                                "status": "uncertain",
                                "detail": "Upload unconfirmed; it will not be blindly repeated.",
                            }
                        )
                        continue
                    content_id = uploaded.content_id
                    with _database(settings.data_dir) as db:
                        db.execute(
                            "INSERT OR REPLACE INTO catalog_tv_content VALUES(?,?,?,?)",
                            (profile.ip, digest, content_id, artwork_id),
                        )
                        if not manual:
                            db.execute(
                                "INSERT OR REPLACE INTO catalog_daily_owned VALUES(?,?,?,?)",
                                (profile.ip, content_id, tv, time.time()),
                            )
                    # A successful upload/selection remains usable even when
                    # thumbnail evidence is unavailable. Without that evidence
                    # do not claim ownership or reuse its potentially recycled ID.
                    self._record_fingerprint(settings, profile, content_id)
                if not manual and held():
                    results.append(
                        {
                            "tv": tv,
                            "status": "deferred",
                            "detail": "A newer manual choice is held today.",
                        }
                    )
                    continue
                mutation_started = True
                if not switch_art(profile, content_id, require_art_mode=True):
                    results.append(
                        {
                            "tv": tv,
                            "status": "uncertain",
                            "detail": "Selection was not confirmed; no automatic repeat was sent.",
                        }
                    )
                    continue
                LibraryStore(settings.data_dir).record_display(
                    job_id=None,
                    content_id=content_id,
                    tv_target=tv,
                    source="catalog-manual" if manual else "daily-art",
                )
                results.append({"tv": tv, "status": "displayed", "artwork_id": artwork_id})
                if not manual:
                    self._prune_owned(settings, profile, content_id)
            except Exception:
                results.append(
                    {
                        "tv": tv,
                        "status": "uncertain" if mutation_started else "error",
                        "detail": "Delivery unconfirmed; TV playback was not forced.",
                    }
                )
            finally:
                lock.release()
        return results

    @classmethod
    def _prune_owned(cls, settings, profile, current_id: str) -> None:
        """Retain eight daily-uploaded files; never delete manual/unrelated art."""
        from frameart.tv.controller import delete_art, list_art_deduplicated

        try:
            inventory = {str(item.get("content_id", "")) for item in list_art_deduplicated(profile)}
            with _database(settings.data_dir) as db:
                rows = db.execute(
                    "SELECT content_id FROM catalog_daily_owned "
                    "WHERE tv_ip=? ORDER BY used_at DESC",
                    (profile.ip,),
                ).fetchall()
                for row in rows:
                    if row[0] not in inventory:
                        db.execute(
                            "DELETE FROM catalog_daily_owned WHERE tv_ip=? AND content_id=?",
                            (profile.ip, row[0]),
                        )
                        db.execute(
                            "DELETE FROM catalog_tv_content WHERE tv_ip=? AND content_id=?",
                            (profile.ip, row[0]),
                        )
                        db.execute(
                            "DELETE FROM catalog_tv_fingerprints WHERE tv_ip=? AND content_id=?",
                            (profile.ip, row[0]),
                        )
                obsolete = [
                    row[0] for row in rows[8:] if row[0] in inventory and row[0] != current_id
                ][:8]
            for identity in obsolete:
                if not cls._fingerprint_matches(settings, profile, identity):
                    continue
                # A user may protect a previously daily-owned image by choosing
                # it manually while retention is fetching thumbnail evidence.
                with _database(settings.data_dir) as db:
                    owned = db.execute(
                        "SELECT 1 FROM catalog_daily_owned WHERE tv_ip=? AND content_id=?",
                        (profile.ip, identity),
                    ).fetchone()
                if owned and delete_art(profile, [identity], require_art_mode=True):
                    cls._forget_content(settings, profile, identity)
        except Exception:
            # Cleanup cannot turn a confirmed successful display into a retry.
            return
