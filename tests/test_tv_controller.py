"""Tests for defensive TV controller behavior."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import call, patch

import pytest

from frameart.config import TVProfile
from frameart.tv.controller import (
    TVOperationBusyError,
    _run_tv_op,
    _run_with_timeout,
    _tv_operation_gate,
    switch_art,
    wait_for_art,
)


def test_run_with_timeout_returns_without_waiting_for_blocked_worker():
    release = threading.Event()
    started = time.monotonic()
    result, error = _run_with_timeout(release.wait, timeout_sec=0.02)
    elapsed = time.monotonic() - started

    assert result is None
    assert error == "timed out"
    assert elapsed < 0.15
    release.set()
    deadline = time.monotonic() + 0.5
    while (
        any(thread.name == "frameart-tv-op" for thread in threading.enumerate())
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)


def test_expired_queued_reads_never_execute_after_active_operation_finishes():
    profile = TVProfile(ip="192.168.1.201")
    active_started = threading.Event()
    release_active = threading.Event()
    calls: list[str] = []

    def active_operation():
        calls.append("active")
        active_started.set()
        assert release_active.wait(1)

    active = threading.Thread(
        target=lambda: _run_tv_op(profile, active_operation, "active", timeout_sec=1),
    )
    active.start()
    assert active_started.wait(1)

    def expired_read(index: int) -> None:
        with pytest.raises(TVOperationBusyError):
            _run_tv_op(
                profile,
                lambda: calls.append(f"read-{index}"),
                f"read-{index}",
                timeout_sec=0.03,
                priority="read",
            )

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(expired_read, range(20)))
        assert sum(thread.name == "frameart-tv-op" for thread in threading.enumerate()) <= 1

    release_active.set()
    active.join(1)
    assert not active.is_alive()
    assert calls == ["active"]

    _run_tv_op(profile, lambda: calls.append("upload"), "upload", timeout_sec=0.5)
    assert calls == ["active", "upload"]


def test_mutation_waiter_runs_before_queued_read():
    profile = TVProfile(ip="192.168.1.202")
    active_started = threading.Event()
    release_active = threading.Event()
    order: list[str] = []

    def active_operation():
        active_started.set()
        assert release_active.wait(1)

    active = threading.Thread(
        target=lambda: _run_tv_op(profile, active_operation, "active", timeout_sec=1),
    )
    active.start()
    assert active_started.wait(1)

    reader = threading.Thread(
        target=lambda: _run_tv_op(
            profile,
            lambda: order.append("read"),
            "read",
            timeout_sec=1,
            priority="read",
        ),
    )
    mutation = threading.Thread(
        target=lambda: _run_tv_op(
            profile,
            lambda: order.append("mutation"),
            "mutation",
            timeout_sec=1,
        ),
    )
    reader.start()
    mutation.start()

    gate = _tv_operation_gate(profile)
    deadline = time.monotonic() + 1
    while gate._waiting_mutations < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert gate._waiting_mutations == 1

    release_active.set()
    active.join(1)
    reader.join(1)
    mutation.join(1)

    assert order == ["mutation", "read"]


@patch("frameart.tv.controller._run_art_call")
def test_switch_art_skips_enable_when_art_mode_is_already_on(mock_call):
    profile = TVProfile(ip="192.168.1.100")
    mock_call.side_effect = ["on", None]

    assert switch_art(profile, "MY_F0006") is True

    descriptions = [invocation.args[2] for invocation in mock_call.call_args_list]
    assert descriptions == ["Get art mode status", "Switch art to MY_F0006"]


@patch("frameart.tv.controller._run_art_call")
def test_switch_art_gives_art_mode_and_selection_independent_calls(mock_call):
    profile = TVProfile(ip="192.168.1.100")
    mock_call.side_effect = ["off", None, None]

    assert switch_art(profile, "MY_F0006") is True

    descriptions = [invocation.args[2] for invocation in mock_call.call_args_list]
    assert descriptions == [
        "Get art mode status",
        "Enable art mode",
        "Switch art to MY_F0006",
    ]


@patch("frameart.tv.controller._run_art_call")
def test_switch_art_reconciles_selection_timeout_as_success(mock_call):
    profile = TVProfile(ip="192.168.1.100")

    def operation(_profile, _func, description, **_kwargs):
        if description == "Get art mode status":
            return "on"
        if description == "Switch art to MY_F0006":
            raise RuntimeError("timed out")
        if description == "Get current artwork":
            return {"content_id": "MY_F0006"}
        raise AssertionError(description)

    mock_call.side_effect = operation

    assert switch_art(profile, "MY_F0006") is True


@patch("frameart.tv.controller._run_art_call")
def test_switch_art_returns_false_when_reconciliation_does_not_match(mock_call):
    profile = TVProfile(ip="192.168.1.100")

    def operation(_profile, _func, description, **_kwargs):
        if description == "Get art mode status":
            return True
        if description == "Switch art to MY_F0006":
            raise RuntimeError("timed out")
        if description == "Get current artwork":
            return {"content_id": "MY_F0005"}
        raise AssertionError(description)

    mock_call.side_effect = operation

    assert switch_art(profile, "MY_F0006") is False


@patch("frameart.tv.controller._run_art_call")
def test_wait_for_art_accepts_content_as_soon_as_it_is_listed(mock_call):
    profile = TVProfile(ip="192.168.1.100")

    def run_callback(_profile, callback, _description, **_kwargs):
        art = type("Art", (), {"available": lambda self: [{"content_id": "MY_F0006"}]})()
        return callback(art)

    mock_call.side_effect = run_callback

    assert wait_for_art(profile, "MY_F0006") is True
    assert mock_call.call_args == call(
        profile,
        mock_call.call_args.args[1],
        "Wait for artwork MY_F0006",
        timeout_sec=3.0,
    )
