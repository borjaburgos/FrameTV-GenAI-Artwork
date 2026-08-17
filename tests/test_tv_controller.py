"""Tests for defensive TV controller behavior."""

from __future__ import annotations

import time

from frameart.tv.controller import _run_with_timeout


def test_run_with_timeout_returns_without_waiting_for_blocked_worker():
    started = time.monotonic()
    result, error = _run_with_timeout(lambda: time.sleep(0.3), timeout_sec=0.02)
    elapsed = time.monotonic() - started

    assert result is None
    assert error == "timed out"
    assert elapsed < 0.15

