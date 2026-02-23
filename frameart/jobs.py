"""Async job queue — submit pipeline work and poll for results.

Jobs are stored in-memory and executed in a background thread pool.
They do not survive server restarts (acceptable for v1).

Completed/failed jobs are evicted after ``MAX_COMPLETED_JOBS`` to
prevent unbounded memory growth.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

MAX_COMPLETED_JOBS = 200


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
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    completed_at: float | None = None


class JobStore:
    """Thread-safe in-memory job store with a background executor.

    Parameters
    ----------
    max_workers:
        Number of threads available for running jobs concurrently.
    max_completed:
        Maximum number of finished (completed/failed) jobs to keep.
        Oldest finished jobs are evicted when this limit is exceeded.
    """

    def __init__(self, max_workers: int = 2, max_completed: int = MAX_COMPLETED_JOBS) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._max_completed = max_completed

    def submit(
        self,
        job_id: str,
        func: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        request_summary: dict[str, Any] | None = None,
    ) -> Job:
        """Submit a function to run in the background.

        Returns the Job immediately (status=pending).
        """
        kwargs = kwargs or {}
        job = Job(id=job_id, request=request_summary or {})
        with self._lock:
            self._jobs[job_id] = job

        self._executor.submit(self._run, job, func, args, kwargs)
        logger.info("Submitted job %s", job_id)
        return job

    def get(self, job_id: str) -> Job | None:
        """Look up a job by ID. Returns None if not found."""
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[Job]:
        """Return the most recent jobs (newest first)."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def _evict_old_jobs(self) -> None:
        """Remove oldest finished jobs when the store exceeds the limit.

        Must be called while holding ``self._lock``.
        """
        finished = [
            j for j in self._jobs.values()
            if j.status in (JobStatus.completed, JobStatus.failed)
        ]
        if len(finished) <= self._max_completed:
            return
        finished.sort(key=lambda j: j.created_at)
        to_remove = len(finished) - self._max_completed
        for job in finished[:to_remove]:
            self._jobs.pop(job.id, None)

    def _run(
        self,
        job: Job,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        job.status = JobStatus.running
        job.started_at = time.monotonic()
        try:
            result = func(*args, **kwargs)
            if hasattr(result, "error") and result.error:
                job.status = JobStatus.failed
                job.error = result.error
            else:
                job.status = JobStatus.completed
            job.result = result
        except Exception as exc:
            job.status = JobStatus.failed
            job.error = str(exc)
            logger.exception("Job %s failed: %s", job.id, exc)
        finally:
            job.completed_at = time.monotonic()
            with self._lock:
                self._evict_old_jobs()


# Module-level singleton used by the API server.
job_store = JobStore()
