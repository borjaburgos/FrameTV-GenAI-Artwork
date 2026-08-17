"""Tests for the core pipeline: prompt normalization and generate flow."""

from __future__ import annotations

import json
from unittest.mock import patch

from frameart.config import STYLE_PRESETS, Settings, TVProfile
from frameart.pipeline import normalize_edit_prompt, normalize_prompt, run_apply
from frameart.tv.controller import UploadResult


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
def test_apply_persists_upload_id_and_fails_when_display_is_not_confirmed(
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
    mock_switch.assert_called_once_with(
        settings.tvs["livingroom"],
        "MY_F0006",
        wait_for_ready=True,
    )
