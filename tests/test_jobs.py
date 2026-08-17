"""Tests for the async job queue."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event

import pytest

from frameart.jobs import JobQueueFullError, JobStatus, JobStore


class TestJobStore:
    def test_submit_and_get(self):
        store = JobStore(max_workers=1)
        job = store.submit("j1", lambda: 42, request_summary={"x": 1})
        assert job.id == "j1"
        assert job.request == {"x": 1}

        # Wait for completion
        for _ in range(50):
            j = store.get("j1")
            if j and j.status in (JobStatus.completed, JobStatus.failed):
                break
            time.sleep(0.05)

        j = store.get("j1")
        assert j is not None
        assert j.status == JobStatus.completed
        assert j.result == 42

    def test_get_missing_returns_none(self):
        store = JobStore(max_workers=1)
        assert store.get("nonexistent") is None

    def test_failed_job(self):
        store = JobStore(max_workers=1)

        def _fail():
            raise ValueError("boom")

        store.submit("j-fail", _fail)

        for _ in range(50):
            j = store.get("j-fail")
            if j and j.status in (JobStatus.completed, JobStatus.failed):
                break
            time.sleep(0.05)

        j = store.get("j-fail")
        assert j is not None
        assert j.status == JobStatus.failed
        assert "boom" in j.error

    def test_list_jobs(self):
        store = JobStore(max_workers=1)
        store.submit("a", lambda: 1)
        store.submit("b", lambda: 2)

        jobs = store.list_jobs()
        ids = [j.id for j in jobs]
        assert "a" in ids
        assert "b" in ids

    def test_pipeline_error_field_marks_failed(self):
        """If the callable returns an object with .error set, mark the job failed."""
        store = JobStore(max_workers=1)

        class FakeResult:
            error = "provider timeout"

        store.submit("j-err", FakeResult)

        for _ in range(50):
            j = store.get("j-err")
            if j and j.status in (JobStatus.completed, JobStatus.failed):
                break
            time.sleep(0.05)

        j = store.get("j-err")
        assert j is not None
        assert j.status == JobStatus.failed
        assert j.error == "provider timeout"

    def test_evicts_oldest_completed_jobs(self):
        """Old completed jobs are evicted when max_completed is exceeded."""
        store = JobStore(max_workers=1, max_completed=2)

        for i in range(4):
            store.submit(f"ev-{i}", lambda: 1)

        # Wait for all to finish
        for _ in range(100):
            all_done = all(
                (j := store.get(f"ev-{i}")) is None
                or j.status in (JobStatus.completed, JobStatus.failed)
                for i in range(4)
            )
            if all_done:
                break
            time.sleep(0.05)

        # At most max_completed (2) finished jobs should remain
        remaining = [j for j in store.list_jobs() if j.status == JobStatus.completed]
        assert len(remaining) <= 2

    def test_rejects_work_when_active_queue_is_full(self):
        store = JobStore(max_workers=1, max_active=1)
        release = Event()
        store.submit("blocked", lambda: release.wait(2))

        with pytest.raises(JobQueueFullError, match="queue is full"):
            store.submit("overflow", lambda: 2)

        release.set()

    def test_completed_jobs_survive_store_restart(self, tmp_path):
        database = tmp_path / "frameart.sqlite3"
        store = JobStore(max_workers=1, database_path=database)
        store.submit("persisted", lambda: {"job_id": "persisted", "value": 42})

        for _ in range(50):
            job = store.get("persisted")
            if job and job.status == JobStatus.completed:
                break
            time.sleep(0.05)

        restarted = JobStore(max_workers=1, database_path=database)
        recovered = restarted.get("persisted")
        assert recovered is not None
        assert recovered.status == JobStatus.completed
        assert recovered.result == {"job_id": "persisted", "value": 42}
        assert Path(database).stat().st_mode & 0o777 == 0o600

    def test_restart_marks_interrupted_jobs_failed(self, tmp_path):
        database = tmp_path / "frameart.sqlite3"
        release = Event()
        store = JobStore(max_workers=1, database_path=database)
        store.submit("interrupted", lambda: release.wait(2))

        for _ in range(50):
            job = store.get("interrupted")
            if job and job.status == JobStatus.running:
                break
            time.sleep(0.01)

        restarted = JobStore(max_workers=1, database_path=database)
        recovered = restarted.get("interrupted")
        assert recovered is not None
        assert recovered.status == JobStatus.failed
        assert recovered.error == "Interrupted by a server restart."
        release.set()
