"""Persistent, reversible feedback and its evidence in a household art profile."""

from __future__ import annotations

import copy
from unittest.mock import patch

from frameart.art_feedback import ArtworkFeedbackStore
from frameart.art_profile import ArtProfileRequest, ArtProfileStore


def _quiz():
    return {
        "version": 1,
        "pairs": [{
            "id": "first",
            "left": {"tags": ["landscape", "calm"]},
            "right": {"tags": ["abstract"]},
        }],
    }


def test_current_vote_persists_and_repeated_taps_are_idempotent(tmp_path):
    store = ArtworkFeedbackStore(tmp_path)
    artwork = {
        "title": "A watercolor landscape",
        "tags": ["landscape", "Landscape"],
        "style": "watercolor",
        "source_url": "https://example.com/artwork/one",
    }
    with patch("frameart.art_feedback.time.time", return_value=100):
        first = store.rate("job:one", "good", artwork)
    with patch("frameart.art_feedback.time.time", return_value=200):
        repeated = store.rate("job:one", "good", artwork)

    assert first == repeated
    assert ArtworkFeedbackStore(tmp_path).get_rating("job:one") == first
    summary = store.summary()
    assert summary["total_count"] == 1
    assert summary["good_count"] == 1
    assert summary["bad_count"] == 0
    assert summary["classified_count"] == 1
    assert summary["preferred_tags"] == [
        {"tag": "landscape", "score": 3},
        {"tag": "watercolor", "score": 3},
    ]
    assert summary["ratings"][0]["artwork"] == artwork


def test_changing_and_clearing_vote_recomputes_without_residual_scores(tmp_path):
    store = ArtworkFeedbackStore(tmp_path)
    artwork = {"title": "Garden", "artist": "Claude Monet", "tags": ["garden"]}
    original = store.rate("museum:garden", "good", artwork)
    changed = store.rate("museum:garden", "bad", artwork)
    assert changed["created_at"] == original["created_at"]
    assert changed["value"] == "bad"
    summary = store.summary()
    assert summary["good_count"] == 0
    assert summary["bad_count"] == 1
    assert summary["preferred_tags"] == []
    assert summary["avoided_tags"] == [
        {"tag": "artist:claude_monet", "score": 3},
        {"tag": "garden", "score": 3},
    ]

    assert store.rate("museum:garden", None, {}) is None
    assert store.get_rating("museum:garden") is None
    assert store.list_ratings() == []
    assert store.summary()["total_count"] == 0
    assert store.summary()["avoided_tags"] == []


def test_unknown_artwork_is_remembered_without_guessing_features(tmp_path):
    store = ArtworkFeedbackStore(tmp_path)
    assert store.enrich_profile(None, _quiz()) is None
    artwork = {
        "title": "Summer landscape",
        "prompt": "A red abstract watercolor of a beach",
        "tv_profile_id": "living-room",
        "content_id": "MY_F0001",
        "artist": "Unknown artist",
    }
    store.rate("tv:living-room:MY_F0001", "good", artwork)
    summary = store.summary()
    assert summary["total_count"] == 1
    assert summary["classified_count"] == 0
    assert summary["preferred_tags"] == []
    assert summary["avoided_tags"] == []
    assert summary["ratings"][0]["artwork"] == artwork
    profile = store.enrich_profile(None, _quiz())
    assert profile["status"] == "draft"
    assert profile["choices"] == {}
    assert profile["derived"]["has_feedback"] is True
    assert profile["derived"]["ready_for_review"] is False
    assert "do not guess" in " ".join(profile["derived"]["summary"])


def test_artwork_ratings_combine_with_quiz_without_mutating_original_answers(tmp_path):
    quiz = _quiz()
    profiles = ArtProfileStore(tmp_path)
    original = profiles.save_profile(
        ArtProfileRequest(
            choices={"first": "left"},
            household={"location": "Family region", "notes": "Our own instructions"},
        ),
        quiz,
    )
    unchanged = copy.deepcopy(original)
    ratings = ArtworkFeedbackStore(tmp_path)
    ratings.rate("job:one", "bad", {"title": "Forest", "tags": ["landscape"]})
    enriched = ratings.enrich_profile(original, quiz)

    assert original == unchanged
    assert profiles.get_profile() == original
    assert enriched["choices"] == original["choices"]
    assert enriched["household"] == original["household"]
    assert enriched["quiz_derived"] == original["derived"]
    assert enriched["derived"]["preferred_tags"] == [{"tag": "calm", "score": 2}]
    assert enriched["derived"]["avoided_tags"] == [{"tag": "landscape", "score": 1}]
    assert enriched["feedback"]["bad_count"] == 1
    assert ratings.enrich_profile(enriched, quiz) == enriched

    profiles.save_profile(ArtProfileRequest(), quiz)
    assert ratings.get_rating("job:one")["value"] == "bad"
    reset = ratings.enrich_profile(profiles.get_profile(), quiz)
    assert reset["choices"] == {}
    assert reset["feedback"]["bad_count"] == 1
    assert reset["derived"]["avoided_tags"] == [{"tag": "landscape", "score": 3}]


def test_summary_uses_all_current_votes_when_recent_export_is_bounded(tmp_path):
    store = ArtworkFeedbackStore(tmp_path)
    for index in range(101):
        store.rate(f"job:{index}", "good", {"title": f"Art {index}", "tags": ["garden"]})
    summary = store.summary()
    assert summary["total_count"] == 101
    assert summary["good_count"] == 101
    assert summary["preferred_tags"] == [{"tag": "garden", "score": 303}]
    assert len(summary["ratings"]) == 100
    assert len(store.list_ratings(limit=5)) == 5
    assert len(store.list_ratings(limit=1000)) == 100
