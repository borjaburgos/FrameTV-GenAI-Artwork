"""Tests for the async job queue."""

from __future__ import annotations

import time

from frameart.jobs import JobStatus, JobStore


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
