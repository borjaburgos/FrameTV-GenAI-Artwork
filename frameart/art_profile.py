"""Locally saved household art preferences learned from a visual quiz."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

MIN_VISUAL_ANSWERS = 6
QUIZ_PATH = Path(__file__).parent / "static" / "curation" / "quiz.json"
QuizPairId = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]


def load_art_quiz() -> dict:
    """Read the bundled, licensed visual examples without contacting a provider."""
    return json.loads(QUIZ_PATH.read_text(encoding="utf-8"))


class HouseholdArtPreferences(BaseModel):
    """Optional details supplied by the household, never inferred from other data."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field("", max_length=100)
    location: str = Field("", max_length=1000)
    roots: str = Field("", max_length=1000)
    kids: str = Field("", max_length=1000)
    room_palette: str = Field("", max_length=1000)
    favorite_subjects: str = Field("", max_length=1000)
    avoid_subjects: str = Field("", max_length=1000)
    meaningful_dates: str = Field("", max_length=1000)
    notes: str = Field("", max_length=1000)
    timezone: str = Field("", max_length=100)
    seasonal: Literal["yes", "some", "no"] = "some"
    adventure: Literal["familiar", "balanced", "adventurous"] = "balanced"
    fit: Literal["preserve", "crop"] = "preserve"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("Choose a valid time zone, such as America/New_York.") from exc
        return value


class ArtProfileRequest(BaseModel):
    """Full quiz snapshot; draft saves are allowed before every question is answered."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    status: Literal["draft", "complete"] = "draft"
    step: int = Field(0, ge=0, le=20)
    choices: dict[QuizPairId, Literal["left", "right", "both", "neither", "skip"]] = Field(
        default_factory=dict, max_length=40
    )
    household: HouseholdArtPreferences = Field(default_factory=HouseholdArtPreferences)


def derive_art_preferences(quiz: dict, request: ArtProfileRequest) -> dict:
    """Keep voting evidence separate from written preferences and artwork reviews."""
    pairs = {pair["id"]: pair for pair in quiz["pairs"]}
    unknown = set(request.choices) - set(pairs)
    if unknown:
        raise ValueError(
            "Some quiz choices are no longer available. Reload the quiz and try again."
        )
    answered_count = sum(choice != "skip" for choice in request.choices.values())
    if request.status == "complete" and answered_count < MIN_VISUAL_ANSWERS:
        raise ValueError(
            f"Choose an answer for at least {MIN_VISUAL_ANSWERS} visual questions "
            "before completing your Art Profile. Your draft can be saved at any time."
        )

    scores: dict[str, float] = defaultdict(float)
    for pair_id, choice in request.choices.items():
        pair = pairs[pair_id]
        if choice in {"left", "right"}:
            for tag in set(pair[choice].get("tags", [])):
                scores[tag] += 2
        elif choice in {"both", "neither"}:
            weight = 1 if choice == "both" else -1
            for side in ("left", "right"):
                for tag in set(pair[side].get("tags", [])):
                    scores[tag] += weight

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
    summary = []
    if preferred:
        summary.append("Your visual choices lean toward " + ", ".join(
            item["tag"].replace("_", " ") for item in preferred[:6]
        ) + ".")
    if avoided:
        summary.append("Your neither choices suggest using less " + ", ".join(
            item["tag"].replace("_", " ") for item in avoided[:4]
        ) + ".")
    fit = request.household.fit
    summary.append(
        "Preserve the whole artwork with a border or matte when needed."
        if fit == "preserve"
        else "Thoughtful crops are welcome when they fit the TV better."
    )
    summary.append({
        "familiar": "Favor familiar subjects and styles, with occasional small surprises.",
        "balanced": "Balance familiar favorites with a few discoveries.",
        "adventurous": "Make room for unusual styles, bold compositions, and discoveries.",
    }[request.household.adventure])
    return {
        "preferred_tags": preferred,
        "avoided_tags": avoided,
        "summary": summary,
        "answered_count": answered_count,
        "ready_for_review": request.status == "complete",
    }


class ArtProfileStore:
    """One household profile alongside the rest of FrameArt's local data."""

    def __init__(self, data_dir: Path) -> None:
        self.database_path = Path(data_dir) / "frameart.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS household_art_profile (
                       id INTEGER PRIMARY KEY CHECK (id = 1),
                       profile_json TEXT NOT NULL
                   )"""
            )
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def get_profile(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT profile_json FROM household_art_profile WHERE id = 1"
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_profile(self, request: ArtProfileRequest, quiz: dict) -> dict:
        derived = derive_art_preferences(quiz, request)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT profile_json FROM household_art_profile WHERE id = 1"
            ).fetchone()
            previous = json.loads(row[0]) if row else {}
            profile = {
                **request.model_dump(),
                "quiz_version": quiz["version"],
                "created_at": previous.get("created_at", now),
                "updated_at": now,
                "completed_at": (
                    previous.get("completed_at") or now
                    if request.status == "complete" else None
                ),
                "derived": derived,
            }
            connection.execute(
                """INSERT INTO household_art_profile (id, profile_json) VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET profile_json = excluded.profile_json""",
                (json.dumps(profile),),
            )
        return profile
