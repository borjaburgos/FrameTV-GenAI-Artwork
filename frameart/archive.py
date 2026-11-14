"""Append-only generated-art archive, with a local durable outbox.

Generation/deletion only use local storage. NAS I/O belongs to the separate
``frameart archive run`` process, normally invoked by a one-minute timer.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from frameart.config import Settings
from frameart.logging_utils import sanitize_log_value

logger = logging.getLogger(__name__)
_FILES = {"source.png", "edited.png", "final.png", "meta.json"}
_KEY = re.compile(r"^\d{4}/\d{2}/\d{2}/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_FIELDS = {
    "job_id",
    "operation",
    "prompt_original",
    "prompt_normalized",
    "style",
    "edit_prompt_original",
    "edit_prompt_normalized",
    "provider",
    "model",
    "source_width",
    "source_height",
    "edited_source_width",
    "edited_source_height",
    "final_width",
    "final_height",
    "upscaler",
    "seed",
    "steps",
    "guidance",
    "negative_prompt",
    "created_at",
    "generation_succeeded",
    "revised_prompt",
    "size_requested",
    "postprocess_steps",
}


class ArchiveConflictError(RuntimeError):
    """An immutable archive path already contains different bytes."""


class ArchiveUnavailableError(RuntimeError):
    """The expected NAS mount/identity is unavailable."""


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return (
            hashlib.file_digest(stream, "sha256").hexdigest()
            if hasattr(hashlib, "file_digest")
            else _stream_digest(stream)
        )


def _stream_digest(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def _key(value: str) -> str:
    if not _KEY.fullmatch(value):
        raise ValueError("Invalid archive generation key.")
    datetime.strptime(value[:10], "%Y/%m/%d")
    return value


def _regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Archive inputs must be regular files, never symbolic links.")


def _plain_path(path: Path) -> Path:
    path = path.absolute()
    if path.resolve() != path:
        raise ValueError("Archive paths must not contain symbolic links or traversal.")
    return path


def _publish(source: Path, destination: Path) -> None:
    """Linux atomic rename with RENAME_NOREPLACE; never fall back to overwrite."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "Atomic no-replace rename requires Linux renameat2.")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _metadata(settings: Settings, raw: dict) -> dict:
    secrets = [config.api_key for config in settings.providers.values() if config.api_key]
    secrets += [config.api_key for config in settings.upscalers.values() if config.api_key]
    for name in ("admin_token", "automation_token", "resourcespace_api_key"):
        value = getattr(settings, name, None)
        if value:
            secrets.append(value.get_secret_value())
    secrets += [
        value
        for name, value in os.environ.items()
        if re.search(r"(?:TOKEN|PASSWORD|SECRET|API_KEY)$", name)
    ]
    result = {}
    for name in _FIELDS:
        value = raw.get(name)
        if isinstance(value, str):
            result[name] = sanitize_log_value(value, secrets=secrets)
        elif value is None or type(value) in (int, float, bool):
            result[name] = value
        elif name == "postprocess_steps" and isinstance(value, list) and all(
            isinstance(step, str) for step in value
        ):
            result[name] = [sanitize_log_value(step, secrets=secrets) for step in value]
    # Deliberately exclude raw provider responses, URLs, arbitrary nested
    # metadata, settings, error strings, credentials and delivery/TV details.
    return result


def _eligible(raw: dict) -> bool:
    return bool(
        isinstance(raw, dict)
        and isinstance(raw.get("provider"), str)
        and raw["provider"]
        and (
            raw.get("prompt_original")
            or raw.get("prompt_normalized")
            or raw.get("operation") == "edit"
        )
    )


def _manifest(entry: Path, expected_key: str) -> dict:
    path = entry / "manifest.json"
    _regular(path)
    value = json.loads(path.read_bytes())
    if (
        not isinstance(value, dict)
        or value.get("format_version") != 1
        or value.get("generation_key") != _key(expected_key)
    ):
        raise ValueError("Invalid archive manifest identity/version.")
    files = value.get("files")
    if not isinstance(files, dict) or not {"source.png", "final.png", "meta.json"} <= files.keys():
        raise ValueError("Archive manifest is incomplete.")
    for name, record in files.items():
        if name not in _FILES or not isinstance(record, dict):
            raise ValueError("Unsafe archive manifest member.")
        if (
            not _HASH.fullmatch(str(record.get("sha256", "")))
            or type(record.get("bytes")) is not int
            or record["bytes"] < 0
        ):
            raise ValueError("Invalid archive checksum/size.")
    return value


def _verify(entry: Path, key: str) -> dict:
    manifest = _manifest(entry, key)
    for name, record in manifest["files"].items():
        path = entry / name
        _regular(path)
        if path.stat().st_size != record["bytes"] or _digest(path) != record["sha256"]:
            raise ArchiveConflictError("Archive checksum mismatch; no file was overwritten.")
    return manifest


def _mounts() -> list[tuple[Path, str, str]]:
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

    result = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        fields, tail = before.split(), after.split()
        result.append((Path(unescape(fields[4])), tail[0], unescape(tail[1])))
    return result


@contextmanager
def _nas(settings: Settings):
    """Pin the verified network directory open, preventing local mount fallback."""
    if not settings.archive_path or not settings.archive_mount_source or not settings.archive_id:
        raise ArchiveUnavailableError("Archive destination identity is not configured.")
    root = _plain_path(settings.archive_path)
    candidates = [
        (mount, fs, source)
        for mount, fs, source in _mounts()
        if root == mount or root.is_relative_to(mount)
    ]
    if not candidates:
        raise ArchiveUnavailableError("Expected NAS mount is absent.")
    _, filesystem, source = max(candidates, key=lambda item: len(item[0].parts))
    if filesystem not in {"cifs", "nfs", "nfs4"} or source != settings.archive_mount_source:
        raise ArchiveUnavailableError("Expected NAS mount is absent or has changed identity.")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # A pinned descriptor continues to refer to this NAS filesystem even if
        # its mountpoint disappears. Never create the destination or mount it.
        pinned = Path(f"/proc/self/fd/{descriptor}")
        if os.fstat(descriptor).st_dev != root.stat().st_dev:
            raise ArchiveUnavailableError("NAS mount changed while opening it.")
        marker = pinned / ".frameart-archive-id"
        _regular(marker)
        if marker.stat().st_size > 256 or marker.read_text().strip() != settings.archive_id:
            raise ArchiveUnavailableError("NAS archive marker does not match.")
        yield pinned
    finally:
        os.close(descriptor)


def _directories(root: Path, key: str, *, create: bool) -> Path:
    current = root
    for part in _key(key).split("/"):
        current = current / part
        if create:
            current.mkdir(exist_ok=True, mode=0o750)
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError("Archive directories must not be symbolic links.")
    return current


def _copy_immutable(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        _regular(destination)
        if source.stat().st_size != destination.stat().st_size or _digest(source) != _digest(
            destination
        ):
            raise ArchiveConflictError("Archive path already contains different bytes.")
        return
    descriptor, name = tempfile.mkstemp(prefix=".frameart-partial-", dir=destination.parent)
    partial = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if _digest(source) != _digest(partial):
            raise ArchiveConflictError("Transfer verification failed.")
        os.chmod(partial, 0o640)
        try:
            _publish(partial, destination)
        except FileExistsError:
            _regular(destination)
            if _digest(source) != _digest(destination):
                raise ArchiveConflictError("Concurrent archive publication conflict.") from None
        _sync_directory(destination.parent)
    finally:
        # Only our unpublished temporary transfer may be removed, never an
        # archived file, generation directory or pre-existing destination.
        partial.unlink(missing_ok=True)


class ArchiveStore:
    """Local queue and receipts; methods other than run/verify/restore are NAS-free."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = _plain_path(settings.data_dir / "archive")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.outbox = self.root / "outbox"
        self.outbox.mkdir(exist_ok=True, mode=0o700)
        _plain_path(self.outbox)
        self.database = self.root / "queue.sqlite3"
        if self.database.is_symlink():
            raise ValueError("Archive queue must not be a symbolic link.")
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS generations (
                generation_key TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', queued_at REAL NOT NULL,
                verified_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, error_code TEXT)""")
        os.chmod(self.database, 0o600)

    def _connect(self):
        return sqlite3.connect(self.database, timeout=5)

    @contextmanager
    def _lock(self, name: str, *, blocking: bool = True):
        descriptor = os.open(self.root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield
        finally:
            os.close(descriptor)

    def stage(self, job_dir: Path) -> str | None:
        """Snapshot completed generated artwork locally before it can be deleted."""
        job_dir = _plain_path(job_dir)
        key = _key(
            job_dir.relative_to(_plain_path(self.settings.data_dir / "artifacts")).as_posix()
        )
        _regular(job_dir / "meta.json")
        raw = json.loads((job_dir / "meta.json").read_bytes())
        if not _eligible(raw):
            return None
        for name in ("source.png", "final.png"):
            _regular(job_dir / name)
        with self._lock("enqueue.lock"):
            with self._connect() as connection:
                record = connection.execute(
                    "SELECT state FROM generations WHERE generation_key=?", (key,)
                ).fetchone()
            if record:
                if record[0] != "verified":
                    _verify(self.outbox / key, key)
                return key
            destination = self.outbox / key
            _plain_path(destination)
            if destination.exists():
                # Recover a snapshot published just before a process crash.
                _verify(destination, key)
            else:
                with tempfile.TemporaryDirectory(prefix=".enqueue-", dir=self.root) as temp:
                    stage = Path(temp) / "entry"
                    stage.mkdir(mode=0o700)
                    for name in sorted(_FILES - {"meta.json"}):
                        source = job_dir / name
                        if source.exists() or source.is_symlink():
                            _regular(source)
                            shutil.copyfile(source, stage / name)
                    (stage / "meta.json").write_bytes(_json_bytes(_metadata(self.settings, raw)))
                    manifest = {
                        "format_version": 1,
                        "generation_key": key,
                        "files": {
                            path.name: {"bytes": path.stat().st_size, "sha256": _digest(path)}
                            for path in sorted(stage.iterdir())
                        },
                    }
                    (stage / "manifest.json").write_bytes(_json_bytes(manifest))
                    for path in stage.iterdir():
                        with path.open("rb") as stream:
                            os.fsync(stream.fileno())
                    _sync_directory(stage)
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    _publish(stage, destination)
                    _sync_directory(destination.parent)
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO generations(generation_key,manifest_sha256,queued_at) "
                    "VALUES(?,?,?)",
                    (key, _digest(destination / "manifest.json"), time.time()),
                )
        return key

    def backfill(self, *, limit: int | None = None, dry_run: bool = False) -> dict:
        result = {"eligible": 0, "skipped": 0, "errors": 0}
        for meta in sorted((self.settings.data_dir / "artifacts").glob("*/*/*/*/meta.json")):
            if limit is not None and result["eligible"] >= limit:
                break
            try:
                _plain_path(meta)
                _regular(meta)
                raw = json.loads(meta.read_bytes())
                if not _eligible(raw):
                    result["skipped"] += 1
                    continue
                for name in ("source.png", "final.png"):
                    _regular(meta.parent / name)
                _key(meta.parent.relative_to(self.settings.data_dir / "artifacts").as_posix())
                if not dry_run:
                    self.stage(meta.parent)
                result["eligible"] += 1
            except (OSError, ValueError, TypeError):
                result["errors"] += 1
        return result

    def status(self) -> dict:
        with self._connect() as connection:
            counts = dict(
                connection.execute("SELECT state,count(*) FROM generations GROUP BY state")
            )
            last = connection.execute("SELECT max(verified_at) FROM generations").fetchone()[0]
            oldest = connection.execute(
                "SELECT min(queued_at) FROM generations WHERE state!='verified'"
            ).fetchone()[0]
            errors = dict(
                connection.execute(
                    "SELECT error_code,count(*) FROM generations WHERE error_code IS NOT NULL "
                    "GROUP BY error_code"
                )
            )
        return {
            "enabled": self.settings.archive_enabled,
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "verified": counts.get("verified", 0),
            "last_verified_at": last,
            "oldest_pending_at": oldest,
            "errors": errors,
        }

    def run(self, *, limit: int = 25, discover: bool = True, retry_now: bool = False) -> dict:
        """Copy independently of the web process; a second worker exits harmlessly."""
        if not self.settings.archive_enabled:
            raise ValueError("Archive is disabled.")
        with self._lock("worker.lock", blocking=False):
            discovery = self.backfill() if discover else None
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT generation_key,manifest_sha256 FROM generations "
                    "WHERE state!='verified' AND retry_at<=? ORDER BY queued_at LIMIT ?",
                    (float("inf") if retry_now else time.time(), limit),
                ).fetchall()
            for key, checksum in rows:
                try:
                    entry = _plain_path(self.outbox / _key(key))
                    _verify(entry, key)
                    if _digest(entry / "manifest.json") != checksum:
                        raise ArchiveConflictError("Local outbox manifest changed.")
                    with _nas(self.settings) as root:
                        destination = _directories(root, key, create=True)
                        for name in sorted(_manifest(entry, key)["files"]):
                            _copy_immutable(entry / name, destination / name)
                        # Manifest is the commit marker and must be published last.
                        _copy_immutable(entry / "manifest.json", destination / "manifest.json")
                        _verify(destination, key)
                        if _digest(destination / "manifest.json") != checksum:
                            raise ArchiveConflictError("Archive manifest differs.")
                    with self._connect() as connection:
                        connection.execute(
                            "UPDATE generations SET state='verified',verified_at=?,"
                            "attempts=attempts+1,"
                            "error_code=NULL,retry_at=0 WHERE generation_key=?",
                            (time.time(), key),
                        )
                except (OSError, ValueError, RuntimeError) as exc:
                    code = (
                        "conflict"
                        if isinstance(exc, ArchiveConflictError)
                        else "nas_unavailable"
                        if isinstance(exc, ArchiveUnavailableError)
                        else "copy_failed"
                    )
                    with self._connect() as connection:
                        connection.execute(
                            "UPDATE generations SET state='failed',attempts=attempts+1,"
                            "error_code=?,"
                            "retry_at=? WHERE generation_key=?",
                            (code, time.time() + 300, key),
                        )
                    logger.warning("Generated-art archive deferred: %s", code)
                else:
                    # Cleanup cannot revoke a verified receipt. Only this
                    # local outbox entry, never NAS files or original images.
                    try:
                        shutil.rmtree(entry)
                    except OSError:
                        logger.warning("Verified archive outbox cleanup deferred.")
            return {"discovery": discovery, **self.status()}

    def verify(self, key: str) -> dict:
        with _nas(self.settings) as root:
            entry = _directories(root, key, create=False)
            manifest = _verify(entry, key)
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT manifest_sha256 FROM generations WHERE generation_key=?", (key,)
                ).fetchone()
            if row and _digest(entry / "manifest.json") != row[0]:
                raise ArchiveConflictError("Archive differs from the local receipt.")
            return {"generation_key": key, "files": len(manifest["files"]), "verified": True}

    def restore(self, key: str, output: Path) -> dict:
        """Recover into a NEW offline folder; never alter the live library or TVs."""
        output = _plain_path(output)
        data_dir = self.settings.data_dir.resolve()
        if output.exists() or output.is_relative_to(data_dir) or data_dir.is_relative_to(output):
            raise ValueError("Restore requires a new directory outside live FrameArt data.")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.verify(key)
        with _nas(self.settings) as root:
            entry = _directories(root, key, create=False)
            manifest = _verify(entry, key)
            with tempfile.TemporaryDirectory(
                prefix=".frameart-restore-", dir=output.parent
            ) as temp:
                staged = Path(temp) / "restored"
                staged.mkdir(mode=0o700)
                for name in (*manifest["files"], "manifest.json"):
                    shutil.copyfile(entry / name, staged / name)
                _verify(staged, key)
                if _digest(staged / "manifest.json") != _digest(entry / "manifest.json"):
                    raise ArchiveConflictError("Archive changed during restore.")
                _publish(staged, output)
        return {"generation_key": key, "files": len(manifest["files"]), "restored": True}


def protect_before_delete(settings: Settings, job_dir: Path) -> None:
    """A local queue failure must not allow deletion of an unprotected generation."""
    if settings.archive_enabled:
        ArchiveStore(settings).stage(job_dir)


def enqueue_generation(settings: Settings, job_dir: Path) -> None:
    """Local-only best effort; archive errors must never turn generation red."""
    if settings.archive_enabled:
        try:
            ArchiveStore(settings).stage(job_dir)
        except Exception:
            logger.error("Could not stage generated-art archive; worker discovery will retry.")
