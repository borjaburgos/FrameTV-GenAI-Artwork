"""Evidence, persistence, and access boundaries for the household visual quiz."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from frameart.api import app
from frameart.art_profile import (
    ArtProfileRequest,
    ArtProfileStore,
    derive_art_preferences,
    load_art_quiz,
)


@pytest.fixture
def quiz():
    return {
        "version": 1,
        "pairs": [
            {
                "id": f"pair-{index}",
                "left": {"tags": ["landscape", "quiet"]},
                "right": {"tags": ["abstract", "bold"]},
            }
            for index in range(12)
        ],
    }


def test_profile_persists_draft_and_completion_then_recomputes_choices(tmp_path, quiz):
    store = ArtProfileStore(tmp_path)
    assert store.get_profile() is None

    draft = store.save_profile(
        ArtProfileRequest(
            choices={"pair-0": "left"},
            step=1,
            household={"location": "New England", "roots": "Spain"},
        ),
        quiz,
    )
    assert draft["status"] == "draft"
    assert draft["completed_at"] is None
    assert draft["derived"]["ready_for_review"] is False
    assert draft["derived"]["preferred_tags"] == [
        {"tag": "landscape", "score": 2},
        {"tag": "quiet", "score": 2},
    ]
    assert draft["derived"]["avoided_tags"] == []

    completed = store.save_profile(
        ArtProfileRequest(
            status="complete",
            choices={f"pair-{index}": "right" for index in range(6)},
            household={"fit": "crop", "adventure": "adventurous"},
        ),
        quiz,
    )
    assert completed["created_at"] == draft["created_at"]
    assert completed["completed_at"] is not None
    assert completed["derived"]["ready_for_review"] is True
    assert completed["derived"]["answered_count"] == 6
    assert completed["derived"]["preferred_tags"] == [
        {"tag": "abstract", "score": 12},
        {"tag": "bold", "score": 12},
    ]
    assert ArtProfileStore(tmp_path).get_profile() == completed
    assert "New England" not in str(completed)


def test_neither_is_a_soft_negative_and_skips_do_not_count(quiz):
    result = derive_art_preferences(
        quiz,
        ArtProfileRequest(choices={"pair-0": "neither", "pair-1": "skip"}),
    )
    assert result["answered_count"] == 1
    assert result["preferred_tags"] == []
    assert {item["tag"] for item in result["avoided_tags"]} == {
        "landscape", "quiet", "abstract", "bold"
    }
    assert "suggest" in result["summary"][0]


def test_cannot_complete_without_enough_visual_evidence(quiz):
    with pytest.raises(ValueError, match="at least 6"):
        derive_art_preferences(
            quiz,
            ArtProfileRequest(
                status="complete", choices={f"pair-{index}": "skip" for index in range(12)}
            ),
        )
    with pytest.raises(ValueError, match="no longer available"):
        derive_art_preferences(quiz, ArtProfileRequest(choices={"unknown": "both"}))


def test_profile_rejects_unbounded_or_unexpected_household_values():
    with pytest.raises(ValidationError):
        ArtProfileRequest(household={"location": "x" * 1001})
    with pytest.raises(ValidationError):
        ArtProfileRequest(choices={"pair-0": "anything"})
    with pytest.raises(ValidationError):
        ArtProfileRequest(household={"timezone": "Moon/Crater"})
    with pytest.raises(ValidationError):
        ArtProfileRequest(derived={"ready_for_review": True})


def test_profile_routes_save_and_load_without_a_provider_or_tv(tmp_path, monkeypatch, quiz):
    monkeypatch.setenv("FRAMEART_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "false")
    client = TestClient(app)
    with patch("frameart.api.load_art_quiz", return_value=quiz):
        assert client.get("/curation/quiz").json() == quiz
        empty = client.get("/curation/profile")
        assert empty.json() is None
        assert empty.headers["cache-control"] == "private, no-store"
        saved = client.put(
            "/curation/profile",
            json={"choices": {"pair-0": "both"}, "household": {"kids": "6 and 9"}},
        )
        assert saved.status_code == 200
        assert saved.headers["cache-control"] == "private, no-store"
        assert saved.json()["household"]["kids"] == "6 and 9"
        assert client.get("/curation/profile").json() == saved.json()
        invalid = client.put(
            "/curation/profile", json={"status": "complete", "choices": {}}
        )
        assert invalid.status_code == 422
        assert client.get("/curation/profile").json() == saved.json()


def test_private_household_profile_requires_admin_access(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEART_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "true")
    client = TestClient(app)
    with patch(
        "frameart.api._authentication_context",
        return_value={"scopes": {"read", "control"}},
    ):
        assert client.get("/curation/profile").status_code == 403
        assert client.put("/curation/profile", json={}).status_code == 403


def test_bundled_visual_quiz_has_licensed_small_local_images():
    quiz = load_art_quiz()
    assert quiz["version"] == 1
    assert len(quiz["pairs"]) == 12
    assert len({pair["id"] for pair in quiz["pairs"]}) == 12
    static_dir = Path(__file__).parents[1] / "frameart" / "static"
    artwork_dir = (static_dir / "curation").resolve()
    total_bytes = 0
    image_paths = set()
    for pair in quiz["pairs"]:
        for side in ("left", "right"):
            artwork = pair[side]
            assert artwork["is_public_domain"] is True
            assert "public domain" in artwork["license"].lower() or (
                "cc0" in artwork["license"].lower()
            )
            assert urlsplit(artwork["source_url"]).scheme == "https"
            assert urlsplit(artwork["source_url"]).hostname
            image_url = artwork["image_url"]
            assert image_url.startswith("/static/curation/")
            image_path = (static_dir / image_url.removeprefix("/static/")).resolve()
            assert image_path.is_relative_to(artwork_dir)
            assert image_path.is_file()
            with Image.open(image_path) as image:
                image.load()
                assert image.format == "WEBP"
                assert max(image.size) <= 640
            image_paths.add(image_path)
            total_bytes += image_path.stat().st_size
    assert len(image_paths) == 24
    assert total_bytes < 3 * 1024 * 1024
