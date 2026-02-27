"""Tests for the core pipeline: prompt normalization and generate flow."""

from __future__ import annotations

from frameart.config import STYLE_PRESETS
from frameart.pipeline import normalize_edit_prompt, normalize_prompt


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
