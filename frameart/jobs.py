"""Bounded async job queue with restart-safe SQLite status history."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from frameart.logging_utils import safe_exception_message

logger = logging.getLogger(__name__)

MAX_COMPLETED_JOBS = 200
MAX_ACTIVE_JOBS = 50


class JobQueueFullError(RuntimeError):
    """Raised when the bounded queue cannot accept more work."""


class JobStatus(str, Enum):
    """Lifecycle states for a job."""

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


@dataclass
class Job:
    """A tracked background job."""

    id: str
    status: JobStatus = JobStatus.pending
    request: dict[str, Any] = field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (Path, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


class JobStore:
    """Thread-safe job executor with an optional SQLite persistence layer."""

    def __init__(
        self,
        max_workers: int = 2,
        max_completed: int = MAX_COMPLETED_JOBS,
        max_active: int = MAX_ACTIVE_JOBS,
        database_path: Path | None = None,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._max_completed = max_completed
        self._max_active = max_active
        self._database_path: Path | None = None
        if database_path is not None:
            self.configure(database_path)

    def configure(self, database_path: Path | None) -> None:
        """Select a database and recover its bounded job history."""
        if database_path is None:
            with self._lock:
                self._database_path = None
            return
        path = Path(database_path)
        with self._lock:
            if self._database_path == path:
                return
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._database_path = path
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS async_jobs (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        result_json TEXT,
                        error TEXT,
                        created_at REAL NOT NULL,
                        started_at REAL,
                        completed_at REAL
                    )
                    """
                )
                connection.execute(
                    """
                    UPDATE async_jobs
                    SET status = ?, error = ?, completed_at = ?
                    WHERE status IN (?, ?)
                    """,
                    (
                        JobStatus.failed.value,
                        "Interrupted by a server restart.",
                        time.time(),
                        JobStatus.pending.value,
                        JobStatus.running.value,
                    ),
                )
                rows = connection.execute(
                    "SELECT * FROM async_jobs ORDER BY created_at DESC"
                ).fetchall()
            os.chmod(path, 0o600)
            self._jobs = {row["id"]: self._job_from_row(row) for row in rows}
            self._evict_old_jobs()

    def _connect(self) -> sqlite3.Connection:
        if self._database_path is None:
            raise RuntimeError("Job persistence database is not configured.")
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        result_json = row["result_json"]
        return Job(
            id=row["id"],
            status=JobStatus(row["status"]),
            request=json.loads(row["request_json"] or "{}"),
            result=json.loads(result_json) if result_json else None,
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def _persist_job(self, job: Job) -> None:
        if self._database_path is None:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO async_jobs (
                    id, status, request_json, result_json, error,
                    created_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    request_json = excluded.request_json,
                    result_json = excluded.result_json,
                    error = excluded.error,
                    created_at = excluded.created_at,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at
                """,
                (
                    job.id,
                    job.status.value,
                    json.dumps(_jsonable(job.request)),
                    json.dumps(_jsonable(job.result)) if job.result is not None else None,
                    job.error,
                    job.created_at,
                    job.started_at,
                    job.completed_at,
                ),
            )

    def submit(
        self,
        job_id: str,
        func: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        request_summary: dict[str, Any] | None = None,
    ) -> Job:
        """Submit a function and return its persisted pending job immediately."""
        kwargs = kwargs or {}
        job = Job(id=job_id, request=request_summary or {})
        with self._lock:
            active = sum(
                existing.status in (JobStatus.pending, JobStatus.running)
                for existing in self._jobs.values()
            )
            if active >= self._max_active:
                raise JobQueueFullError(
                    f"Job queue is full ({self._max_active} active jobs); retry later"
                )
            if job_id in self._jobs:
                raise ValueError(f"Job {job_id!r} already exists")
            self._jobs[job_id] = job
            self._persist_job(job)
        try:
            self._executor.submit(self._run, job, func, args, kwargs)
        except Exception:
            self.delete(job_id)
            raise
        logger.info("Submitted job %s", job_id)
        return job

    def get(self, job_id: str) -> Job | None:
        """Look up a job by ID."""
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[Job]:
        """Return the most recent jobs, newest first."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)
        return jobs[:limit]

    def delete(self, job_id: str) -> None:
        """Delete a terminal job record."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status in (JobStatus.pending, JobStatus.running):
                return
            self._jobs.pop(job_id, None)
            if self._database_path is not None:
                with self._connect() as connection:
                    connection.execute("DELETE FROM async_jobs WHERE id = ?", (job_id,))

    def _evict_old_jobs(self) -> None:
        finished = [
            job
            for job in self._jobs.values()
            if job.status in (JobStatus.completed, JobStatus.failed)
        ]
        if len(finished) <= self._max_completed:
            return
        finished.sort(key=lambda job: job.created_at)
        stale = finished[: len(finished) - self._max_completed]
        stale_ids = [job.id for job in stale]
        for job_id in stale_ids:
            self._jobs.pop(job_id, None)
        if self._database_path is not None and stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            with self._connect() as connection:
                connection.execute(
                    f"DELETE FROM async_jobs WHERE id IN ({placeholders})",  # noqa: S608
                    stale_ids,
                )

    def _run(
        self,
        job: Job,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        with self._lock:
            job.status = JobStatus.running
            job.started_at = time.time()
            self._persist_job(job)
        result: Any | None = None
        status = JobStatus.completed
        error: str | None = None
        try:
            result = func(*args, **kwargs)
            if hasattr(result, "error") and result.error:
                status = JobStatus.failed
                error = result.error
        except Exception as exc:
            status = JobStatus.failed
            error = safe_exception_message(exc)
            logger.error("Job %s failed: %s", job.id, error)
        with self._lock:
            # Publish the terminal in-memory state and its durable row atomically.
            # Readers must never observe completion before restart recovery can.
            job.result = result
            job.status = status
            job.error = error
            job.completed_at = time.time()
            self._persist_job(job)
            self._evict_old_jobs()


job_store = JobStore()
