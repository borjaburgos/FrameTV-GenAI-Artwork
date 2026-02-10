"""Artifact store — save generated images, metadata, and logs.

Directory layout (under data_dir):
    artifacts/YYYY/MM/DD/<job_id>/
        source.png      — raw output from the image provider
        final.png       — post-processed 3840x2160 image
        meta.json       — full metadata
    logs/
        frameart.log
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def generate_job_id() -> str:
    """Generate a unique job ID."""
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    return f"{ts}-{short_uuid}"


def get_job_dir(data_dir: Path, job_id: str) -> Path:
    """Return the artifact directory for a job, creating it if needed."""
    now = datetime.now(timezone.utc)
    job_dir = data_dir / "artifacts" / now.strftime("%Y/%m/%d") / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def save_source_image(job_dir: Path, image_bytes: bytes) -> Path:
    """Save the raw source image from the provider."""
    path = job_dir / "source.png"
    path.write_bytes(image_bytes)
    logger.info("Saved source image: %s (%d bytes)", path, len(image_bytes))
    return path


def save_final_image(job_dir: Path, image_bytes: bytes) -> Path:
    """Save the post-processed final image."""
    path = job_dir / "final.png"
    path.write_bytes(image_bytes)
    logger.info("Saved final image: %s (%d bytes)", path, len(image_bytes))
    return path


def save_metadata(job_dir: Path, metadata: dict[str, Any]) -> Path:
    """Save job metadata as JSON."""
    path = job_dir / "meta.json"
    with open(path, "w") as f:
        json.dump(metadata, f, indent=2, default=str)
    logger.info("Saved metadata: %s", path)
    return path


def load_metadata(job_dir: Path) -> dict[str, Any]:
    """Load metadata from a job directory."""
    path = job_dir / "meta.json"
    with open(path) as f:
        return json.load(f)


def setup_logging(data_dir: Path, level: str = "INFO", log_file: str | None = None) -> None:
    """Configure application logging."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_file or str(log_dir / "frameart.log")

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Root logger config
    root_logger = logging.getLogger("frameart")
    root_logger.setLevel(numeric_level)

    # File handler
    fh = logging.FileHandler(log_path)
    fh.setLevel(numeric_level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    root_logger.addHandler(fh)

    # Stream handler (for CLI --verbose/--debug)
    sh = logging.StreamHandler()
    sh.setLevel(numeric_level)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root_logger.addHandler(sh)
