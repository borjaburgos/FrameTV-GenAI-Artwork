"""One explicit background catalog refresh per data directory, with progress."""

from __future__ import annotations

import threading
from pathlib import Path

from frameart.art_catalog import sync_catalog
from frameart.resourcespace import ResourceSpaceError

_guard = threading.Lock()
_jobs: dict[str, dict] = {}


def sync_progress(settings) -> dict | None:
    with _guard:
        job = _jobs.get(str(Path(settings.data_dir).resolve()))
        return dict(job) if job else None


def start_sync(settings) -> dict:
    key = str(Path(settings.data_dir).resolve())
    with _guard:
        if _jobs.get(key, {}).get("status") == "running":
            return dict(_jobs[key])
        _jobs[key] = {"status": "running", "completed": 0, "total": 0, "error": None}

    def progress(completed, total):
        with _guard:
            _jobs[key].update(completed=completed, total=total)

    def run():
        try:
            result = sync_catalog(settings, progress=progress)
            with _guard:
                _jobs[key].update(
                    status="completed", completed=result["item_count"], total=result["item_count"]
                )
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, ResourceSpaceError)
                else ("Catalog refresh failed; the previous cached collection was retained.")
            )
            with _guard:
                _jobs[key].update(status="error", error=message)

    threading.Thread(target=run, name="catalog-sync", daemon=True).start()
    return sync_progress(settings)
