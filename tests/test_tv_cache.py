"""Tests for persistent TV metadata and thumbnail caches."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from frameart.api import _cached_tv_mattes, _cached_tv_thumbnail
from frameart.config import TVProfile
from frameart.tv.cache import TVCacheStore
from frameart.tv.controller import TVOperationBusyError


def test_cache_persists_mattes_and_thumbnails_across_instances(tmp_path):
    first = TVCacheStore(tmp_path)
    first.set_mattes("tv-a", [{"matte_id": "shadowbox_polar"}])
    first.set_thumbnail("tv-a", "MY_F0001", b"jpeg-data")

    restarted = TVCacheStore(tmp_path)

    assert restarted.get_mattes("tv-a").value == [{"matte_id": "shadowbox_polar"}]
    assert restarted.get_thumbnail("tv-a", "MY_F0001").value["data"] == b"jpeg-data"


def test_empty_matte_results_are_not_cached(tmp_path):
    cache = TVCacheStore(tmp_path)

    assert cache.set_mattes("tv-a", []) is None
    assert cache.get_mattes("tv-a") is None


def test_thumbnail_cache_is_bounded_and_keys_do_not_mix_tvs(tmp_path):
    cache = TVCacheStore(tmp_path, max_thumbnail_entries=2, max_thumbnail_bytes=100)
    cache.set_thumbnail("tv-a", "MY_F0001", b"first")
    time.sleep(0.002)
    cache.set_thumbnail("tv-b", "MY_F0001", b"second")
    time.sleep(0.002)
    cache.set_thumbnail("tv-a", "MY_F0002", b"third")

    assert cache.get_thumbnail("tv-a", "MY_F0001") is None
    assert cache.get_thumbnail("tv-b", "MY_F0001").value["data"] == b"second"
    assert cache.get_thumbnail("tv-a", "MY_F0002").value["data"] == b"third"


def test_delete_thumbnails_removes_only_requested_tv_entries(tmp_path):
    cache = TVCacheStore(tmp_path)
    cache.set_thumbnail("tv-a", "MY_F0001", b"first")
    cache.set_thumbnail("tv-b", "MY_F0001", b"second")

    cache.delete_thumbnails("tv-a", ["MY_F0001"])

    assert cache.get_thumbnail("tv-a", "MY_F0001") is None
    assert cache.get_thumbnail("tv-b", "MY_F0001") is not None


@patch("frameart.tv.controller.get_matte_list")
def test_concurrent_matte_cache_misses_are_coalesced(mock_mattes, tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path)
    profile = TVProfile(ip="192.168.1.210")

    def upstream(_profile):
        time.sleep(0.05)
        return [{"matte_id": "shadowbox_polar"}]

    mock_mattes.side_effect = upstream
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _index: _cached_tv_mattes(settings, profile)[0].value,
                range(8),
            )
        )

    assert all(result == [{"matte_id": "shadowbox_polar"}] for result in results)
    mock_mattes.assert_called_once()


@patch("frameart.tv.controller.get_art_thumbnail")
def test_same_thumbnail_cache_misses_are_coalesced(mock_thumbnail, tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path)
    profile = TVProfile(ip="192.168.1.211")

    def upstream(_profile, _content_id):
        time.sleep(0.05)
        return b"jpeg"

    mock_thumbnail.side_effect = upstream
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _index: _cached_tv_thumbnail(
                    settings,
                    profile,
                    "MY_F0001",
                )[0].value["data"],
                range(8),
            )
        )

    assert results == [b"jpeg"] * 8
    mock_thumbnail.assert_called_once()


@patch("frameart.tv.controller.get_art_thumbnail")
def test_fourteen_cold_thumbnails_admit_only_bounded_upstream_calls(
    mock_thumbnail,
    tmp_path,
):
    settings = SimpleNamespace(data_dir=tmp_path)
    profile = TVProfile(ip="192.168.1.212")
    release = threading.Event()
    two_started = threading.Event()
    started = 0
    started_lock = threading.Lock()

    def upstream(_profile, content_id):
        nonlocal started
        with started_lock:
            started += 1
            if started == 2:
                two_started.set()
        assert release.wait(1)
        return f"jpeg-{content_id}".encode()

    mock_thumbnail.side_effect = upstream

    def fetch(index: int):
        try:
            return _cached_tv_thumbnail(settings, profile, f"MY_F{index:04d}")
        except TVOperationBusyError:
            return None

    with ThreadPoolExecutor(max_workers=14) as executor:
        futures = [executor.submit(fetch, index) for index in range(14)]
        assert two_started.wait(1)
        time.sleep(0.08)
        release.set()
        results = [future.result() for future in futures]

    assert mock_thumbnail.call_count == 2
    assert sum(result is not None for result in results) == 2
