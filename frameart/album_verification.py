"""Conservative source-anchored recovery of acknowledged album uploads.

A content ID, inventory entry or old HTTP cache is not identity evidence. Only
a fresh, pairing-stable TV thumbnail closely matching the cached uploaded photo
can establish a missing baseline. This never sends a TV mutation.
"""

from __future__ import annotations

import io
import warnings

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat


def _agrees(expected: Image.Image, actual: Image.Image) -> bool:
    expected = expected.resize((96, 96), Image.Resampling.LANCZOS)
    if ImageStat.Stat(expected.convert("L")).stddev[0] < 12:
        return False
    expected = expected.filter(ImageFilter.GaussianBlur(1))
    diff = ImageChops.difference(expected, actual)
    stats = ImageStat.Stat(diff)
    if max(stats.mean) > 2 or max(stats.rms) > 4:
        return False
    return all(
        max(ImageStat.Stat(diff.crop((x, y, x + 12, y + 12))).mean) <= 4
        for x in range(0, 96, 12)
        for y in range(0, 96, 12)
    )


def thumbnail_matches_photo(photo: bytes, thumbnail: bytes) -> bool:
    """Allow JPEG noise and Samsung's full-canvas fill crop, not arbitrary zoom.

    Require agreement across the whole RGB image AND every spatial tile. A
    small perceptual hash alone is too weak to claim a recycled content ID.
    A fill crop must retain an entire source axis and at least 25% of its area.
    Only its position on the other axis varies; never search arbitrary patches,
    rotations, zoom levels or stretched images. Low-detail inputs stay uncertain.
    """
    if not 1 <= len(photo) <= 30 * 1024**2 or not 1 <= len(thumbnail) <= 8 * 1024**2:
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(photo)) as source, Image.open(io.BytesIO(thumbnail)) as tv:
                if source.width * source.height > 50_000_000 or tv.width * tv.height > 4_000_000:
                    return False
                if (
                    min(tv.size) < 64
                    or getattr(tv, "n_frames", 1) != 1
                    or getattr(source, "n_frames", 1) != 1
                ):
                    return False
                original = ImageOps.exif_transpose(source).convert("RGB")
                actual = ImageOps.exif_transpose(tv).convert("RGB")
                # Samsung emits 16:9 fill thumbnails even for portrait uploads;
                # its crop is not necessarily centered. Bound the comparison
                # canvas and test ONLY the standard full-axis cover geometry.
                actual.thumbnail((384, 384), Image.Resampling.LANCZOS)
                target = actual.size
                observed = actual.resize((96, 96), Image.Resampling.LANCZOS)
                observed = observed.filter(ImageFilter.GaussianBlur(1))
                expected = ImageOps.pad(original, target, Image.Resampling.LANCZOS, color=(0, 0, 0))
                if _agrees(expected, observed):
                    return True
                scale = max(target[0] / original.width, target[1] / original.height)
                width = max(target[0], round(original.width * scale))
                height = max(target[1], round(original.height * scale))
                if target[0] * target[1] / (width * height) < 0.25:
                    return False
                cover = original.resize((width, height), Image.Resampling.LANCZOS)
                dx, dy = width - target[0], height - target[1]
                extent = max(dx, dy)
                steps = min(extent, 512)
                offsets = {round(i * extent / max(steps, 1)) for i in range(steps + 1)}
                return any(
                    _agrees(
                        cover.crop(
                            (
                                offset if dx else 0,
                                offset if dy else 0,
                                (offset if dx else 0) + target[0],
                                (offset if dy else 0) + target[1],
                            )
                        ),
                        observed,
                    )
                    for offset in sorted(offsets)
                )
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False
