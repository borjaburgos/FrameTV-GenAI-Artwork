"""Tests for consistent data backup and recoverable restore."""

from __future__ import annotations

import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from frameart.backup import create_data_backup, restore_data_backup


def _data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "frameart-data"
    data_dir.mkdir()
    (data_dir / "settings").mkdir()
    (data_dir / "settings" / "managed.yaml").write_text("default_provider: openai\n")
    with sqlite3.connect(data_dir / "frameart.sqlite3") as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
        connection.execute("INSERT INTO example VALUES ('before')")
    return data_dir


def test_backup_and_restore_round_trip_with_safety_snapshot(tmp_path: Path):
    data_dir = _data_dir(tmp_path)
    archive = create_data_backup(data_dir, tmp_path / "backup.tar.gz")
    assert archive.stat().st_mode & 0o777 == 0o600

    (data_dir / "settings" / "managed.yaml").write_text("changed: true\n")
    with sqlite3.connect(data_dir / "frameart.sqlite3") as connection:
        connection.execute("UPDATE example SET value = 'after'")

    safety = restore_data_backup(data_dir, archive)

    assert safety is not None and safety.is_file()
    assert (data_dir / "settings" / "managed.yaml").read_text() == (
        "default_provider: openai\n"
    )
    with sqlite3.connect(data_dir / "frameart.sqlite3") as connection:
        assert connection.execute("SELECT value FROM example").fetchone()[0] == "before"


def test_backup_rejects_symbolic_links(tmp_path: Path):
    data_dir = _data_dir(tmp_path)
    (data_dir / "outside-link").symlink_to(tmp_path / "outside")

    with pytest.raises(ValueError, match="symbolic links"):
        create_data_backup(data_dir, tmp_path / "backup.tar.gz")


def test_in_volume_backup_is_not_nested_and_survives_restore(tmp_path: Path):
    data_dir = _data_dir(tmp_path)
    backups_dir = data_dir / "backups"
    backups_dir.mkdir()
    (backups_dir / "older.tar.gz").write_bytes(b"old")
    archive = create_data_backup(data_dir, backups_dir / "current.tar.gz")
    with tarfile.open(archive, "r:gz") as backup_tar:
        assert not any(
            member.name.startswith("frameart-backup/backups/")
            for member in backup_tar.getmembers()
        )

    (data_dir / "settings" / "managed.yaml").write_text("changed: true\n")
    restore_data_backup(data_dir, archive)

    assert (data_dir / "backups" / "current.tar.gz").is_file()
    assert (data_dir / "settings" / "managed.yaml").read_text() == (
        "default_provider: openai\n"
    )


def test_restore_rejects_path_traversal_archive(tmp_path: Path):
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = b"unsafe"
        member = tarfile.TarInfo("frameart-backup/../../outside")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(ValueError, match="Unsafe backup member path"):
        restore_data_backup(tmp_path / "frameart-data", archive_path)
