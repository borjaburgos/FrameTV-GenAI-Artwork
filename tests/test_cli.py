"""Tests for recovery-oriented command-line behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from frameart.cli import main
from frameart.config import Settings, TVProfile


@patch("frameart.tv.controller.upload_image")
@patch("frameart.tv.controller.switch_art", return_value=True)
@patch("frameart.cli.load_settings")
def test_tv_display_selects_existing_content_without_uploading(
    mock_settings,
    mock_switch,
    mock_upload,
    tmp_path,
):
    profile = TVProfile(ip="192.168.1.100")
    mock_settings.return_value = Settings(
        data_dir=tmp_path,
        tvs={"livingroom": profile},
    )

    result = CliRunner().invoke(
        main,
        ["tv", "display", "--tv", "livingroom", "--content-id", "MY_F0006"],
    )

    assert result.exit_code == 0
    assert "Now displaying MY_F0006" in result.output
    mock_switch.assert_called_once_with(profile, "MY_F0006")
    mock_upload.assert_not_called()


@patch("frameart.pipeline.run_apply")
@patch("frameart.cli.load_settings")
def test_apply_exits_nonzero_and_prints_recovery_id(mock_settings, mock_apply, tmp_path):
    image_path = tmp_path / "art.jpg"
    image_path.write_bytes(b"image")
    mock_settings.return_value = Settings(data_dir=tmp_path)
    mock_apply.return_value = SimpleNamespace(
        error="Upload succeeded, but display failed",
        job_id="job-1",
        source_path=None,
        final_path=None,
        content_id="MY_F0006",
        tv_switched=False,
        timings={},
    )

    result = CliRunner().invoke(
        main,
        ["apply", "--image", str(image_path), "--tv-ip", "192.168.1.100"],
    )

    assert result.exit_code == 1
    assert "TV content ID: MY_F0006" in result.output
