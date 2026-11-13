"""Rating identity, TV isolation, and read-only current artwork API behavior."""

from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from frameart.api import app
from frameart.config import TVProfile
from frameart.library import LibraryStore
from frameart.tv.cache import TVCacheStore, tv_cache_key
from frameart.tv.controller import TVStatus


@pytest.fixture
def feedback_api(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEART_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "false")
    settings = SimpleNamespace(
        data_dir=tmp_path,
        tvs={
            "family": TVProfile(ip="192.168.50.10"),
            "other": TVProfile(ip="192.168.50.11"),
        },
    )
    with patch("frameart.api._settings", return_value=settings):
        yield TestClient(app), settings


def _artifact(settings, job_id, **metadata):
    job_dir = settings.data_dir / "artifacts" / "2026" / "11" / "12" / job_id
    job_dir.mkdir(parents=True)
    image = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
    (job_dir / "final.png").write_bytes(image.getvalue())
    (job_dir / "meta.json").write_text(json.dumps({"job_id": job_id, **metadata}))


def _showing(content_id="MY_F0001"):
    return TVStatus(
        reachable=True, art_mode_supported=True, art_mode_on=True, current_artwork=content_id
    )


def test_current_artwork_never_matches_another_tvs_content_id(feedback_api):
    client, settings = feedback_api
    _artifact(
        settings, "family-art", prompt_original="Family landscape",
        content_id="MY_F0001", tv_ip=settings.tvs["family"].ip,
    )
    _artifact(
        settings, "other-art", prompt_original="Other abstract",
        content_id="MY_F0001", tv_ip=settings.tvs["other"].ip,
    )
    with patch("frameart.tv.controller.get_status", return_value=_showing()):
        family = client.get("/curation/now-showing?tv_profile_id=family").json()
        other = client.get("/curation/now-showing?tv_profile_id=other").json()
    assert family["artwork"]["key"] == "job:family-art"
    assert family["artwork"]["title"] == "Family landscape"
    assert other["artwork"]["key"] == "job:other-art"
    assert other["artwork"]["title"] == "Other abstract"


def test_rating_targets_captured_artwork_without_requerying_tv(feedback_api):
    client, settings = feedback_api
    _artifact(
        settings, "captured-art", prompt_original="Watercolor landscape", style="watercolor",
        tv_content_ids={settings.tvs["family"].ip: "MY_F0001"},
    )
    LibraryStore(settings.data_dir).set_tags("captured-art", ["landscape"])
    with patch("frameart.tv.controller.get_status", return_value=_showing()) as status:
        captured = client.get("/curation/now-showing?tv_profile_id=family").json()
        status.reset_mock()
        status.return_value = _showing("MY_F9999")
        rating = client.put(
            "/curation/ratings",
            json={"reference": captured["artwork"]["reference"], "value": "good"},
        )
        status.assert_not_called()
    assert rating.status_code == 200
    assert rating.json()["artwork"]["key"] == "job:captured-art"
    assert rating.json()["rating"] == "good"
    assert rating.json()["summary"]["good_count"] == 1
    assert rating.headers["cache-control"] == "private, no-store"
    learned = client.get("/curation/profile").json()
    assert learned["feedback"]["good_count"] == 1
    assert learned["feedback"]["classified_count"] == 1


def test_same_museum_work_across_tvs_has_one_changeable_vote(feedback_api):
    client, settings = feedback_api
    for job_id, profile_id in [("museum-family", "family"), ("museum-other", "other")]:
        _artifact(
            settings, job_id,
            source_metadata={
                "source": "aic", "artwork_id": "16568", "title": "Water Lilies",
                "artist": "Claude Monet",
            },
            tv_content_ids={settings.tvs[profile_id].ip: "MY_F0001"},
        )
    first = client.put(
        "/curation/ratings", json={"reference": {"job_id": "museum-family"}, "value": "good"}
    )
    assert first.json()["artwork"]["key"] == "catalog:aic:16568"
    changed = client.put(
        "/curation/ratings", json={"reference": {"job_id": "museum-other"}, "value": "bad"}
    )
    assert changed.json()["summary"]["good_count"] == 0
    assert changed.json()["summary"]["bad_count"] == 1
    listed = client.get("/curation/ratings").json()
    assert len(listed["ratings"]) == 1
    cleared = client.put(
        "/curation/ratings", json={"reference": {"job_id": "museum-family"}, "value": None}
    )
    assert cleared.json()["rating"] is None
    assert cleared.json()["summary"]["total_count"] == 0


@pytest.mark.parametrize(
    ("tv_status", "expected"),
    [
        (TVStatus(reachable=False), "offline"),
        (TVStatus(reachable=True, art_mode_supported=True, art_mode_on=False), "watching"),
        (TVStatus(reachable=True, art_mode_supported=False), "unknown"),
    ],
)
def test_non_art_viewing_is_not_ratable_or_mutated(feedback_api, tv_status, expected):
    client, _settings = feedback_api
    with (
        patch("frameart.tv.controller.get_status", return_value=tv_status),
        patch("frameart.tv.controller.upload_image") as upload,
        patch("frameart.tv.controller.switch_art") as switch,
        patch("frameart.tv.controller.delete_art") as delete,
        patch("frameart.tv.controller.get_art_thumbnail") as thumbnail,
    ):
        response = client.get("/curation/now-showing?tv_profile_id=family")
    assert response.status_code == 200
    assert response.json()["status"] == expected
    assert response.json()["artwork"] is None
    assert response.json()["rating"] is None
    upload.assert_not_called()
    switch.assert_not_called()
    delete.assert_not_called()
    thumbnail.assert_not_called()


def test_unknown_tv_art_has_only_scoped_identity_and_cached_preview(feedback_api):
    client, settings = feedback_api
    cache = TVCacheStore(settings.data_dir)
    cache.set_thumbnail(tv_cache_key(settings.tvs["family"]), "MY_F0001", b"cached-preview")
    with (
        patch("frameart.tv.controller.get_status", return_value=_showing()),
        patch("frameart.tv.controller.get_art_thumbnail") as tv_thumbnail,
    ):
        current = client.get("/curation/now-showing?tv_profile_id=family").json()
        preview = client.get(current["artwork"]["thumbnail_url"])
        vote = client.put(
            "/curation/ratings",
            json={"reference": current["artwork"]["reference"], "value": "good"},
        )
    tv_thumbnail.assert_not_called()
    assert preview.content == b"cached-preview"
    assert current["artwork"]["key"].startswith("tv:192.168.50.10:8002:")
    assert current["artwork"]["tags"] == []
    assert vote.json()["summary"]["classified_count"] == 0


def test_rating_rejects_untrusted_metadata_missing_or_mixed_references(feedback_api):
    client, _settings = feedback_api
    for reference in [
        {}, {"job_id": "*"}, {"tv_profile_id": "family"},
        {"job_id": "a", "tv_profile_id": "family", "content_id": "MY_F0001"},
        {"job_id": "a", "tags": ["made-up"]},
        {"job_id": "a", "artwork_key": "job:a"},
    ]:
        assert client.put(
            "/curation/ratings", json={"reference": reference, "value": "good"}
        ).status_code == 422
    assert client.get("/curation/now-showing?tv_profile_id=missing").status_code == 404
    assert client.put(
        "/curation/ratings", json={"reference": {"job_id": "missing"}, "value": "good"}
    ).status_code == 404


def test_saved_rating_can_be_revised_and_cleared_after_source_disappears(feedback_api):
    client, settings = feedback_api
    _artifact(settings, "deleted-source", prompt_original="Old artwork", style="watercolor")
    saved = client.put(
        "/curation/ratings", json={"reference": {"job_id": "deleted-source"}, "value": "good"}
    )
    assert saved.status_code == 200
    job_dir = settings.data_dir / "artifacts" / "2026" / "11" / "12" / "deleted-source"
    (job_dir / "final.png").unlink()
    (job_dir / "meta.json").unlink()
    job_dir.rmdir()
    with patch("frameart.tv.controller.get_status") as status:
        changed = client.put(
            "/curation/ratings",
            json={"reference": {"artwork_key": "job:deleted-source"}, "value": "bad"},
        )
        cleared = client.put(
            "/curation/ratings",
            json={"reference": {"artwork_key": "job:deleted-source"}, "value": None},
        )
    status.assert_not_called()
    assert changed.status_code == 200
    assert changed.json()["artwork"]["title"] == "Old artwork"
    assert changed.json()["summary"]["bad_count"] == 1
    assert cleared.json()["summary"]["total_count"] == 0
    assert client.put(
        "/curation/ratings",
        json={"reference": {"artwork_key": "made-up"}, "value": "good"},
    ).status_code == 404


def test_artwork_feedback_requires_household_admin_access(feedback_api, monkeypatch):
    client, _settings = feedback_api
    monkeypatch.setenv("FRAMEART_AUTH_ENABLED", "true")
    with patch(
        "frameart.api._authentication_context", return_value={"scopes": {"read", "control"}}
    ):
        assert client.get("/curation/ratings").status_code == 403
        assert client.get("/curation/now-showing?tv_profile_id=family").status_code == 403
        assert client.put("/curation/ratings", json={}).status_code == 403
