"""Local, reversible household reactions to individual artworks."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from frameart.art_profile import ArtProfileRequest, derive_art_preferences

FEEDBACK_WEIGHT = 3
MAX_EXPORTED_RATINGS = 100


def _feature_text(value: Any, *, max_length: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().casefold().split())
    if not normalized or len(normalized) > max_length:
        return None
    if normalized in {"unknown", "unknown artist", "none", "n/a"}:
        return None
    return normalized.replace(" ", "_")


def artwork_features(artwork: dict[str, Any]) -> set[str]:
    """Use explicit metadata only; a vote never guesses what an image depicts."""
    tags = artwork.get("tags")
    features = {
        feature
        for value in (tags if isinstance(tags, list) else [])
        if (feature := _feature_text(value, max_length=100))
    }
    if style := _feature_text(artwork.get("style"), max_length=100):
        features.add(style)
    if artist := _feature_text(artwork.get("artist")):
        features.add(f"artist:{artist}")
    return features


def _ranked_preferences(scores: dict[str, float]) -> tuple[list[dict], list[dict]]:
    preferred = [
        {"tag": tag, "score": score}
        for tag, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if score > 0
    ]
    avoided = [
        {"tag": tag, "score": abs(score)}
        for tag, score in sorted(scores.items(), key=lambda item: (item[1], item[0]))
        if score < 0
    ]
    return preferred, avoided


class ArtworkFeedbackStore:
    """One current household vote per artwork, independent of the quiz draft."""

    def __init__(self, data_dir: Path) -> None:
        self.database_path = Path(data_dir) / "frameart.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS artwork_feedback (
                    artwork_key TEXT PRIMARY KEY,
                    value TEXT NOT NULL CHECK (value IN ('good', 'bad')),
                    artwork_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artwork_feedback_updated
                    ON artwork_feedback(updated_at DESC);
                """
            )
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "artwork_key": row["artwork_key"],
            "value": row["value"],
            "artwork": json.loads(row["artwork_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_rating(self, key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artwork_feedback WHERE artwork_key = ?", (key,)
            ).fetchone()
        return self._record(row)

    def rate(
        self,
        key: str,
        value: Literal["good", "bad"] | None,
        artwork: dict[str, Any],
    ) -> dict | None:
        """Replace or clear a vote; repeated taps cannot inflate its influence."""
        if not isinstance(key, str) or not key.strip() or len(key) > 512:
            raise ValueError("Artwork reference is invalid.")
        if value is not None and value not in {"good", "bad"}:
            raise ValueError("Choose good, bad, or clear the rating.")
        if not isinstance(artwork, dict):
            raise ValueError("Artwork metadata is invalid.")
        encoded = json.dumps(artwork, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("Artwork metadata is too large.")
        with self._connect() as connection:
            if value is None:
                connection.execute("DELETE FROM artwork_feedback WHERE artwork_key = ?", (key,))
                return None
            now = time.time()
            connection.execute(
                """INSERT INTO artwork_feedback (
                       artwork_key, value, artwork_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(artwork_key) DO UPDATE SET
                       value = excluded.value,
                       artwork_json = excluded.artwork_json,
                       updated_at = excluded.updated_at
                   WHERE artwork_feedback.value != excluded.value
                      OR artwork_feedback.artwork_json != excluded.artwork_json""",
                (key, value, encoded, now, now),
            )
            row = connection.execute(
                "SELECT * FROM artwork_feedback WHERE artwork_key = ?", (key,)
            ).fetchone()
        return self._record(row)

    def list_ratings(self, limit: int = MAX_EXPORTED_RATINGS) -> list[dict]:
        limit = min(max(int(limit), 1), MAX_EXPORTED_RATINGS)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artwork_feedback ORDER BY updated_at DESC, artwork_key LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def summary(self) -> dict:
        """Recompute from current votes, including those beyond the recent list."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artwork_feedback ORDER BY updated_at DESC, artwork_key"
            ).fetchall()
        scores: dict[str, float] = defaultdict(float)
        good_count = 0
        classified_count = 0
        for row in rows:
            good = row["value"] == "good"
            good_count += good
            features = artwork_features(json.loads(row["artwork_json"]))
            classified_count += bool(features)
            for feature in features:
                scores[feature] += FEEDBACK_WEIGHT if good else -FEEDBACK_WEIGHT
        preferred, avoided = _ranked_preferences(scores)
        return {
            "good_count": good_count,
            "bad_count": len(rows) - good_count,
            "total_count": len(rows),
            "classified_count": classified_count,
            "preferred_tags": preferred,
            "avoided_tags": avoided,
            "ratings": [self._record(row) for row in rows[:MAX_EXPORTED_RATINGS]],
        }

    def enrich_profile(self, profile: dict | None, quiz: dict) -> dict | None:
        """Combine independent rating evidence with original quiz/household answers."""
        feedback = self.summary()
        if profile is None and feedback["total_count"] == 0:
            return None
        profile = copy.deepcopy(profile) if profile else ArtProfileRequest().model_dump()
        request = ArtProfileRequest.model_validate({
            field: profile[field]
            for field in ArtProfileRequest.model_fields
            if field in profile
        })
        quiz_derived = derive_art_preferences(quiz, request)
        scores: dict[str, float] = defaultdict(float)
        for evidence in (quiz_derived, feedback):
            for item in evidence["preferred_tags"]:
                scores[item["tag"]] += item["score"]
            for item in evidence["avoided_tags"]:
                scores[item["tag"]] -= item["score"]
        preferred, avoided = _ranked_preferences(scores)
        derived = copy.deepcopy(quiz_derived)
        derived.update({
            "preferred_tags": preferred,
            "avoided_tags": avoided,
            "feedback_count": feedback["total_count"],
            "has_feedback": feedback["total_count"] > 0,
        })
        if feedback["total_count"]:
            summary = []
            if preferred:
                summary.append("Your choices and artwork ratings lean toward " + ", ".join(
                    item["tag"].replace("_", " ") for item in preferred[:6]
                ) + ".")
            if avoided:
                summary.append("Your reactions suggest using less " + ", ".join(
                    item["tag"].replace("_", " ") for item in avoided[:4]
                ) + ".")
            summary.append(
                f"{feedback['good_count']} liked and {feedback['bad_count']} disliked artworks "
                "are saved as examples for future curation."
            )
            if feedback["classified_count"] < feedback["total_count"]:
                summary.append(
                    "Ratings without descriptive metadata are remembered for the exact artwork; "
                    "they do not guess a style or subject."
                )
            derived["summary"] = [*summary, *quiz_derived["summary"][-2:]]
        profile["quiz_derived"] = quiz_derived
        profile["derived"] = derived
        profile["feedback"] = feedback
        return profile
