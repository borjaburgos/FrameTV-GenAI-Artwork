"""Exercise the actual gate/retry/connection runners with fake TV transports."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from frameart.config import TVProfile
from frameart.tv.controller import (
    TVOperationError,
    _run_art_call,
    _run_tv_op,
    delete_art,
    switch_art,
    upload_image,
)


@pytest.fixture
def profile():
    return TVProfile(ip="192.168.254.210")


def art_transport(*, mode="on", current="MY-CURRENT"):
    art = MagicMock()
    art.get_artmode.return_value = mode
    art.get_current.return_value = {"content_id": current} if current else None
    return art


def assert_no_power_or_mode_setter(*connections):
    for art in connections:
        art.set_artmode.assert_not_called()
        art.power.assert_not_called()
        art.send_key.assert_not_called()


@patch("frameart.tv.controller._connect_art")
def test_guarded_upload_post_send_response_loss_is_attempted_once(mock_connect, profile):
    art = art_transport()
    art.upload.side_effect = RuntimeError("image accepted but reply lost")
    mock_connect.return_value = art

    result = upload_image(profile, b"\xff\xd8" + b"x" * 120, "JPEG", require_art_mode=True)

    assert result.success is False
    assert result.deferred is False
    art.upload.assert_called_once()
    art.get_artmode.assert_called_once()
    mock_connect.assert_called_once()
    art.close.assert_called_once()
    art.select_image.assert_not_called()
    assert_no_power_or_mode_setter(art)


@pytest.mark.parametrize("mode", ["off", None, "unknown", False])
@patch("frameart.tv.controller._connect_art")
def test_guarded_upload_refusal_survives_real_runner_without_retry(mock_connect, profile, mode):
    art = art_transport(mode=mode)
    mock_connect.return_value = art

    result = upload_image(profile, b"\xff\xd8" + b"x" * 120, "JPEG", require_art_mode=True)

    assert result.success is False
    assert result.deferred is True
    art.upload.assert_not_called()
    mock_connect.assert_called_once()
    assert_no_power_or_mode_setter(art)


@patch("frameart.tv.controller._connect_art")
def test_guarded_upload_unknown_mode_exception_is_completed_refusal(mock_connect, profile):
    art = art_transport()
    art.get_artmode.side_effect = RuntimeError("mode unknown")
    mock_connect.return_value = art

    result = upload_image(profile, b"\xff\xd8" + b"x" * 120, "JPEG", require_art_mode=True)

    assert result.deferred is True
    art.get_artmode.assert_called_once()
    art.upload.assert_not_called()
    mock_connect.assert_called_once()


@patch("frameart.tv.controller._connect_art")
def test_guarded_select_post_send_loss_selects_once_then_reconciles_readonly(mock_connect, profile):
    advisory = art_transport()
    selection = art_transport()
    selection.select_image.side_effect = RuntimeError("selection accepted but reply lost")
    reconciliation = art_transport(current="MY-REQUESTED")
    mock_connect.side_effect = [advisory, selection, reconciliation]

    assert switch_art(profile, "MY-REQUESTED", require_art_mode=True) is True

    selection.select_image.assert_called_once_with("MY-REQUESTED")
    reconciliation.get_current.assert_called_once()
    advisory.select_image.assert_not_called()
    reconciliation.select_image.assert_not_called()
    assert mock_connect.call_count == 3
    assert_no_power_or_mode_setter(advisory, selection, reconciliation)


@patch("frameart.tv.controller._connect_art")
def test_guarded_select_unconfirmed_readback_never_repeats_selection(mock_connect, profile):
    advisory = art_transport()
    selection = art_transport()
    selection.select_image.side_effect = RuntimeError("selection reply lost")
    reconciliation = art_transport(current=None)
    mock_connect.side_effect = [advisory, selection, reconciliation]

    assert switch_art(profile, "MY-REQUESTED", require_art_mode=True) is False

    selection.select_image.assert_called_once_with("MY-REQUESTED")
    reconciliation.get_current.assert_called_once()
    assert mock_connect.call_count == 3
    assert_no_power_or_mode_setter(advisory, selection, reconciliation)


@patch("frameart.tv.controller._connect_art")
def test_guarded_select_late_mode_refusal_never_selects_or_reconciles(mock_connect, profile):
    advisory = art_transport()
    selection = art_transport(mode="off")
    mock_connect.side_effect = [advisory, selection]

    assert switch_art(profile, "MY-REQUESTED", require_art_mode=True) is False

    selection.select_image.assert_not_called()
    selection.get_current.assert_not_called()
    assert mock_connect.call_count == 2
    assert_no_power_or_mode_setter(advisory, selection)


@patch("frameart.tv.controller._connect_art")
def test_guarded_delete_post_send_loss_never_repeats_delete(mock_connect, profile):
    art = art_transport(current="MY-CURRENT")
    art.delete_list.side_effect = RuntimeError("deletion accepted but reply lost")
    mock_connect.return_value = art

    assert delete_art(profile, ["MY-OLD"], require_art_mode=True) is False

    art.delete_list.assert_called_once_with(["MY-OLD"])
    mock_connect.assert_called_once()
    art.get_current.assert_called_once()
    assert_no_power_or_mode_setter(art)


@patch("frameart.tv.controller._connect_art")
def test_guarded_delete_current_art_refusal_never_mutates_or_retries(mock_connect, profile):
    art = art_transport(current="MY-OLD")
    mock_connect.return_value = art

    assert delete_art(profile, ["MY-OLD"], require_art_mode=True) is False

    art.delete_list.assert_not_called()
    mock_connect.assert_called_once()
    assert_no_power_or_mode_setter(art)


@patch("frameart.tv.controller.RETRY_BACKOFF", [0, 0, 0])
def test_legacy_read_runner_preserves_three_attempts(profile):
    action = MagicMock(side_effect=[RuntimeError("read1"), RuntimeError("read2"), "ok"])

    assert _run_tv_op(profile, action, "Read status", priority="read") == "ok"

    assert action.call_count == 3


@patch("frameart.tv.controller.RETRY_BACKOFF", [0, 0, 0])
@patch("frameart.tv.controller._connect_art")
def test_legacy_art_read_retries_on_new_connections(mock_connect, profile):
    first, second, third = art_transport(), art_transport(), art_transport()
    first.available.side_effect = RuntimeError("read1")
    second.available.side_effect = RuntimeError("read2")
    third.available.return_value = []
    mock_connect.side_effect = [first, second, third]

    assert _run_art_call(profile, lambda art: art.available(), "Read art", priority="read") == []

    assert mock_connect.call_count == 3
    assert all(art.close.call_count == 1 for art in (first, second, third))
    assert_no_power_or_mode_setter(first, second, third)


@patch("frameart.tv.controller.RETRY_BACKOFF", [0, 0, 0])
@patch("frameart.tv.controller._connect_art")
def test_legacy_explicit_upload_retains_default_retry_behavior(mock_connect, profile):
    art = art_transport()
    art.upload.side_effect = [RuntimeError("legacy1"), RuntimeError("legacy2"), "MY-NEW"]
    mock_connect.return_value = art

    result = upload_image(profile, b"\xff\xd8" + b"x" * 120, "JPEG")

    assert result.success is True
    assert art.upload.call_count == 3
    assert mock_connect.call_count == 3
    art.get_artmode.assert_not_called()


def test_retry_false_runs_mutation_once_through_actual_gate(profile):
    action = MagicMock(side_effect=RuntimeError("reply lost after mutation"))

    with pytest.raises(TVOperationError):
        _run_tv_op(profile, action, "One mutation", retry=False)

    action.assert_called_once()
