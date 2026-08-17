"""Consistent, owner-private FrameArt data backups and recoverable restores."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from frameart import __version__

BACKUP_FORMAT_VERSION = 1
_ROOT_NAME = "frameart-backup"
_DATABASE_FILES = {"frameart.sqlite3", "frameart.sqlite3-wal", "frameart.sqlite3-shm"}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _validate_data_dir(data_dir: Path) -> Path:
    resolved = Path(data_dir).expanduser().resolve()
    broad_roots = {
        Path("/"),
        Path.home().resolve(),
        Path(tempfile.gettempdir()).resolve(),
        Path("/data"),
    }
    if resolved in broad_roots:
        raise ValueError("Refusing to operate on a broad system or home directory.")
    return resolved


def _reject_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Data backups do not follow symbolic links: {path}")


def create_data_backup(data_dir: Path, output: Path | None = None) -> Path:
    """Create a consistent tar.gz backup, including an online SQLite snapshot."""
    source = _validate_data_dir(data_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"FrameArt data directory does not exist: {source}")
    _reject_symlinks(source)

    destination = Path(output or Path.cwd() / f"frameart-backup-{_timestamp()}.tar.gz")
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Backup already exists: {destination}")

    with tempfile.TemporaryDirectory(prefix="frameart-backup-", dir=destination.parent) as temp:
        stage = Path(temp) / _ROOT_NAME

        def ignore_backup_state(directory: str, names: list[str]) -> list[str]:
            ignored = [name for name in names if name in _DATABASE_FILES]
            if Path(directory).resolve() == source:
                ignored.extend(name for name in names if name == "backups")
            return ignored

        shutil.copytree(
            source,
            stage,
            ignore=ignore_backup_state,
        )
        database = source / "frameart.sqlite3"
        if database.is_file():
            with (
                sqlite3.connect(database, timeout=30) as live,
                sqlite3.connect(stage / "frameart.sqlite3") as snapshot,
            ):
                live.backup(snapshot)
            os.chmod(stage / "frameart.sqlite3", 0o600)

        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "frameart_version": __version__,
            "database_included": database.is_file(),
        }
        (stage / "backup-manifest.json").write_text(json.dumps(manifest, indent=2))
        with tarfile.open(destination, "x:gz") as archive:
            archive.add(stage, arcname=_ROOT_NAME, recursive=True)
    os.chmod(destination, 0o600)
    return destination


def _validate_archive(archive: tarfile.TarFile) -> None:
    members = archive.getmembers()
    if not members:
        raise ValueError("Backup archive is empty.")
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"Unsafe backup member path: {member.name}")
        if path.parts[0] != _ROOT_NAME:
            raise ValueError("Backup archive has an unexpected root directory.")
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError(f"Unsupported backup member type: {member.name}")


def restore_data_backup(
    data_dir: Path,
    archive_path: Path,
    *,
    pre_restore_output: Path | None = None,
) -> Path | None:
    """Restore a validated backup and roll back automatically if copying fails.

    The FrameArt API must be stopped before this function is called.
    """
    target = _validate_data_dir(data_dir)
    backup = Path(archive_path).expanduser().resolve()
    if not backup.is_file():
        raise FileNotFoundError(f"Backup archive does not exist: {backup}")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)

    with tempfile.TemporaryDirectory(prefix="frameart-restore-") as temp:
        stage_parent = Path(temp)
        with tarfile.open(backup, "r:gz") as archive:
            _validate_archive(archive)
            archive.extractall(stage_parent)
        stage = stage_parent / _ROOT_NAME
        manifest_path = stage / "backup-manifest.json"
        if not manifest_path.is_file():
            raise ValueError("Backup manifest is missing.")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
            raise ValueError("Backup format is not supported by this FrameArt version.")
        manifest_path.unlink()

        archive_copy: Path | None = None
        if backup.is_relative_to(target):
            archive_copy = stage_parent / backup.name
            shutil.copy2(backup, archive_copy)

        safety_temp: Path | None = None
        if target.is_dir() and any(target.iterdir()):
            safety_temp = stage_parent / f"frameart-pre-restore-{_timestamp()}.tar.gz"
            create_data_backup(target, safety_temp)

        rollback = target / f".restore-rollback-{uuid.uuid4().hex}"
        rollback.mkdir(mode=0o700)
        for child in list(target.iterdir()):
            if child != rollback:
                os.replace(child, rollback / child.name)
        try:
            for child in stage.iterdir():
                destination = target / child.name
                if child.is_dir():
                    shutil.copytree(child, destination)
                else:
                    shutil.copy2(child, destination)
            database = target / "frameart.sqlite3"
            if database.is_file():
                os.chmod(database, 0o600)
        except Exception:
            for child in list(target.iterdir()):
                if child == rollback:
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for child in rollback.iterdir():
                os.replace(child, target / child.name)
            rollback.rmdir()
            raise
        else:
            shutil.rmtree(rollback)

        backups_dir = target / "backups"
        backups_dir.mkdir(mode=0o700, exist_ok=True)
        if archive_copy:
            restored_archive = backups_dir / archive_copy.name
            shutil.copy2(archive_copy, restored_archive)
            os.chmod(restored_archive, 0o600)
        safety_backup: Path | None = None
        if safety_temp:
            safety_backup = Path(
                pre_restore_output
                or backups_dir / safety_temp.name
            ).expanduser().resolve()
            safety_backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safety_temp, safety_backup)
            os.chmod(safety_backup, 0o600)
        return safety_backup
