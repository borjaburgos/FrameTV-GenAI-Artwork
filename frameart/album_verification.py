"""Conservative source-anchored recovery of acknowledged album uploads.

A content ID, inventory entry or old HTTP cache is not identity evidence. Only
a fresh, pairing-stable TV thumbnail closely matching the cached uploaded photo
can establish a missing baseline. This never sends a TV mutation.
"""

from __future__ import annotations

import io
import warnings

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat


def thumbnail_matches_photo(photo: bytes, thumbnail: bytes) -> bool:
    """Allow thumbnail scaling/JPEG noise, not arbitrary crops or flat images.

    Require agreement across the whole RGB image AND every spatial tile. A
    small perceptual hash alone is too weak to claim a recycled content ID.
    Unusual TV crops, low-detail images and undecodable inputs stay uncertain.
    """
    if not 1 <= len(photo) <= 30 * 1024**2 or not 1 <= len(thumbnail) <= 8 * 1024**2:
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(photo)) as source, Image.open(io.BytesIO(thumbnail)) as tv:
                if source.width * source.height > 50_000_000 or tv.width * tv.height > 4_000_000:
                    return False
                if min(tv.size) < 64 or getattr(tv, "n_frames", 1) != 1:
                    return False
                original = ImageOps.exif_transpose(source).convert("RGB")
                actual = ImageOps.exif_transpose(tv).convert("RGB")
                # Only the full photo (possibly with centered black padding)
                # is eligible. Never search for a matching crop inside a photo.
                expected = ImageOps.pad(
                    original, actual.size, Image.Resampling.LANCZOS, color=(0, 0, 0)
                )
                expected = expected.resize((96, 96), Image.Resampling.LANCZOS)
                actual = actual.resize((96, 96), Image.Resampling.LANCZOS)
                if ImageStat.Stat(expected.convert("L")).stddev[0] < 12:
                    return False
                # Suppress subpixel resampler/chroma-subsampling noise, not
                # geometric differences; retain the spatial tile checks below.
                expected = expected.filter(ImageFilter.GaussianBlur(1))
                actual = actual.filter(ImageFilter.GaussianBlur(1))
                diff = ImageChops.difference(expected, actual)
                stats = ImageStat.Stat(diff)
                if max(stats.mean) > 2 or max(stats.rms) > 4:
                    return False
                return all(
                    max(ImageStat.Stat(diff.crop((x, y, x + 12, y + 12))).mean) <= 4
                    for x in range(0, 96, 12)
                    for y in range(0, 96, 12)
                )
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False
