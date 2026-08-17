"""Tests for defensive TV controller behavior."""

from __future__ import annotations

import time
from unittest.mock import call, patch

from frameart.config import TVProfile
from frameart.tv.controller import _run_with_timeout, switch_art, wait_for_art


def test_run_with_timeout_returns_without_waiting_for_blocked_worker():
    started = time.monotonic()
    result, error = _run_with_timeout(lambda: time.sleep(0.3), timeout_sec=0.02)
    elapsed = time.monotonic() - started

    assert result is None
    assert error == "timed out"
    assert elapsed < 0.15


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
