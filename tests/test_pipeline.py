"""Tests for the core pipeline: prompt normalization and generate flow."""

from __future__ import annotations

import json
from unittest.mock import patch

from frameart.config import STYLE_PRESETS, Settings, TVProfile
from frameart.pipeline import (
    PipelineResult,
    normalize_edit_prompt,
    normalize_prompt,
    run_apply,
    run_generate_and_apply,
)
from frameart.tv.controller import TVStatus, UploadResult


class TestNormalizePrompt:
    def test_basic_prompt(self):
        result = normalize_prompt("a sunset", auto_aspect_hint=False)
        assert "a sunset" in result

    def test_with_style_preset(self):
        result = normalize_prompt("a cat", style="abstract", auto_aspect_hint=False)
        assert "a cat" in result
        assert STYLE_PRESETS["abstract"] in result

    def test_with_auto_aspect_hint(self):
        result = normalize_prompt("a mountain", auto_aspect_hint=True)
        assert "16:9" in result
        assert "wide landscape" in result.lower()

    def test_without_auto_aspect_hint(self):
        result = normalize_prompt("a mountain", auto_aspect_hint=False)
        assert "16:9" not in result

    def test_custom_style(self):
        result = normalize_prompt("a tree", style="in neon cyberpunk style", auto_aspect_hint=False)
        assert "neon cyberpunk" in result

    def test_unknown_preset_used_as_custom(self):
        result = normalize_prompt("a river", style="my_custom_style", auto_aspect_hint=False)
        assert "my_custom_style" in result

    def test_strips_whitespace(self):
        result = normalize_prompt("  hello  ", auto_aspect_hint=False)
        assert result.startswith("hello")


class TestNormalizeEditPrompt:
    def test_with_auto_aspect_hint_landscape_source(self):
        result = normalize_edit_prompt(
            "turn this into an oil painting",
            source_width=3000,
            source_height=2000,
            auto_aspect_hint=True,
        )
        assert "16:9" in result
        assert "portrait" not in result.lower()

    def test_with_auto_aspect_hint_portrait_source(self):
        result = normalize_edit_prompt(
            "turn this into an oil painting",
            source_width=1200,
            source_height=2000,
            auto_aspect_hint=True,
        )
        assert "16:9" in result
        assert "portrait" in result.lower()
        assert "recompose" in result.lower()

    def test_without_auto_aspect_hint(self):
        result = normalize_edit_prompt(
            "turn this into an oil painting",
            source_width=1200,
            source_height=2000,
            auto_aspect_hint=False,
        )
        assert "16:9" not in result
        assert "portrait" not in result.lower()

    def test_strips_whitespace(self):
        result = normalize_edit_prompt(
            "  hello  ",
            source_width=1200,
            source_height=2000,
            auto_aspect_hint=True,
        )
        assert result.startswith("hello")


@patch("frameart.pipeline.tv_ctrl.switch_art", return_value=False)
@patch(
    "frameart.pipeline.tv_ctrl.upload_image",
    return_value=UploadResult(content_id="MY_F0006", success=True),
)
@patch(
    "frameart.pipeline.tv_ctrl.preflight_tv",
    return_value=TVStatus(reachable=True, art_mode_supported=True),
)
def test_apply_persists_upload_id_and_fails_when_display_is_not_confirmed(
    mock_preflight,
    mock_upload,
    mock_switch,
    tmp_path,
):
    image_path = tmp_path / "art.jpg"
    image_path.write_bytes(b"uploaded image bytes")
    settings = Settings(
        data_dir=tmp_path,
        tvs={"livingroom": TVProfile(ip="192.168.1.100")},
    )

    result = run_apply(settings, image_path, tv_name="livingroom")

    assert result.content_id == "MY_F0006"
    assert result.tv_switched is False
    assert result.error is not None
    assert "frameart tv display" in result.error
    metadata = json.loads((result.job_dir / "meta.json").read_text())
    assert metadata["content_id"] == "MY_F0006"
    assert metadata["tv_switched"] is False
    assert metadata["error"] == result.error
    mock_upload.assert_called_once()
    mock_preflight.assert_called_once_with(settings.tvs["livingroom"])
    mock_switch.assert_called_once_with(
        settings.tvs["livingroom"],
        "MY_F0006",
        wait_for_ready=True,
    )


@patch("frameart.pipeline.run_generate")
@patch(
    "frameart.pipeline.tv_ctrl.preflight_tv",
    return_value=TVStatus(reachable=False, error="timed out"),
)
def test_generate_and_apply_fails_before_provider_when_tv_is_offline(
    mock_preflight,
    mock_generate,
    tmp_path,
):
    settings = Settings(
        data_dir=tmp_path,
        tvs={"livingroom": TVProfile(ip="192.168.1.100")},
    )

    result = run_generate_and_apply(settings, "paid prompt", tv_name="livingroom")

    assert result.error_code == "tv_unreachable"
    assert result.generation_succeeded is False
    assert result.delivery_status == "not_attempted"
    assert "Generate Anyway" in (result.error or "")
    mock_generate.assert_not_called()
    mock_preflight.assert_called_once_with(settings.tvs["livingroom"])


@patch("frameart.pipeline.run_generate")
@patch("frameart.pipeline.tv_ctrl.preflight_tv")
def test_generate_anyway_skips_tv_preflight(mock_preflight, mock_generate, tmp_path):
    job_dir = tmp_path / "artifacts" / "generated"
    job_dir.mkdir(parents=True)
    final_path = job_dir / "final.png"
    final_path.write_bytes(b"generated")
    mock_generate.return_value = PipelineResult(
        job_id="generated",
        job_dir=job_dir,
        final_path=final_path,
        metadata={"job_id": "generated"},
        generation_succeeded=True,
    )
    settings = Settings(
        data_dir=tmp_path,
        tvs={"livingroom": TVProfile(ip="192.168.1.100")},
    )

    result = run_generate_and_apply(
        settings,
        "paid prompt",
        tv_name="livingroom",
        no_upload=True,
    )

    assert result.error is None
    assert result.generation_succeeded is True
    assert result.delivery_status == "skipped"
    mock_preflight.assert_not_called()


@patch("frameart.pipeline.tv_ctrl.upload_image")
@patch("frameart.pipeline.run_generate")
@patch(
    "frameart.pipeline.tv_ctrl.preflight_tv",
    side_effect=[
        TVStatus(reachable=True, art_mode_supported=True),
        TVStatus(reachable=False, error="network changed"),
    ],
)
def test_generation_artifact_survives_tv_becoming_offline_before_upload(
    mock_preflight,
    mock_generate,
    mock_upload,
    tmp_path,
):
    job_dir = tmp_path / "artifacts" / "generated"
    job_dir.mkdir(parents=True)
    final_path = job_dir / "final.png"
    final_path.write_bytes(b"generated")
    mock_generate.return_value = PipelineResult(
        job_id="generated",
        job_dir=job_dir,
        final_path=final_path,
        metadata={"job_id": "generated"},
        generation_succeeded=True,
    )
    settings = Settings(
        data_dir=tmp_path,
        tvs={"livingroom": TVProfile(ip="192.168.1.100")},
    )

    result = run_generate_and_apply(settings, "paid prompt", tv_name="livingroom")

    assert result.error_code == "tv_unreachable"
    assert result.generation_succeeded is True
    assert result.delivery_status == "failed"
    assert result.final_path == final_path
    assert final_path.exists()
    mock_upload.assert_not_called()
    assert mock_preflight.call_count == 2
    metadata = json.loads((job_dir / "meta.json").read_text())
    assert metadata["error_code"] == "tv_unreachable"
    assert metadata["generation_succeeded"] is True


@patch("frameart.pipeline.tv_ctrl.upload_image")
@patch(
    "frameart.pipeline.tv_ctrl.preflight_tv",
    return_value=TVStatus(reachable=False, error="offline"),
)
def test_apply_fails_before_upload_retry_when_tv_is_offline(
    mock_preflight,
    mock_upload,
    tmp_path,
):
    image_path = tmp_path / "saved.png"
    image_path.write_bytes(b"saved artwork")
    settings = Settings(
        data_dir=tmp_path,
        tvs={"livingroom": TVProfile(ip="192.168.1.100")},
    )

    result = run_apply(settings, image_path, tv_name="livingroom")

    assert result.error_code == "tv_unreachable"
    assert result.generation_succeeded is True
    assert result.delivery_status == "failed"
    mock_upload.assert_not_called()
    mock_preflight.assert_called_once()
