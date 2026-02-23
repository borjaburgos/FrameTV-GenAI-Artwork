"""Tests for TV artwork cleanup logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from frameart.config import TVProfile
from frameart.tv.cleanup import (
    _is_favourite,
    _is_user_upload,
    cleanup_artworks,
)


def _profile() -> TVProfile:
    return TVProfile(ip="192.168.1.100")


def _art(content_id: str, **kwargs) -> dict:
    """Build a minimal artwork dict."""
    return {"content_id": content_id, **kwargs}


# ---------------------------------------------------------------------------
# _is_user_upload
# ---------------------------------------------------------------------------


class TestIsUserUpload:
    def test_my_f_prefix(self):
        assert _is_user_upload(_art("MY_F0001")) is True

    def test_my_dash_f_prefix(self):
        assert _is_user_upload(_art("MY-F0022")) is True

    def test_samsung_art_store(self):
        assert _is_user_upload(_art("SAM-S12345")) is False

    def test_empty_content_id(self):
        assert _is_user_upload({}) is False


# ---------------------------------------------------------------------------
# _is_favourite
# ---------------------------------------------------------------------------


class TestIsFavourite:
    def test_bool_true(self):
        assert _is_favourite(_art("X", is_favourite=True)) is True

    def test_bool_false(self):
        assert _is_favourite(_art("X", is_favourite=False)) is False

    def test_string_true(self):
        assert _is_favourite(_art("X", favourite="true")) is True

    def test_string_false(self):
        assert _is_favourite(_art("X", favourite="false")) is False

    def test_american_spelling(self):
        assert _is_favourite(_art("X", is_favorite=True)) is True

    def test_category_id_favourite(self):
        assert _is_favourite(_art("X", category_id="MY-C0004")) is True

    def test_no_favourite_field(self):
        assert _is_favourite(_art("X")) is False


# ---------------------------------------------------------------------------
# cleanup_artworks
# ---------------------------------------------------------------------------


class TestCleanupArtworks:
    @patch("frameart.tv.cleanup._connect")
    @patch("frameart.tv.cleanup.list_art")
    def test_deletes_oldest_over_limit(self, mock_list, mock_conn):
        """When there are more user artworks than `keep`, delete the oldest."""
        mock_list.return_value = [
            _art("MY_F0001"),
            _art("MY_F0002"),
            _art("MY_F0003"),
            _art("MY_F0004"),
            _art("MY_F0005"),
        ]
        mock_art = MagicMock()
        mock_conn.return_value.art.return_value = mock_art

        result = cleanup_artworks(_profile(), keep=3, order="oldest_first")

        assert result.error is None
        assert result.deleted == ["MY_F0001", "MY_F0002"]
        assert result.kept == 3
        mock_art.delete_list.assert_called_once_with(["MY_F0001", "MY_F0002"])

    @patch("frameart.tv.cleanup._connect")
    @patch("frameart.tv.cleanup.list_art")
    def test_deletes_newest_first(self, mock_list, mock_conn):
        """With order=newest_first, delete the newest instead."""
        mock_list.return_value = [
            _art("MY_F0001"),
            _art("MY_F0002"),
            _art("MY_F0003"),
            _art("MY_F0004"),
        ]
        mock_art = MagicMock()
        mock_conn.return_value.art.return_value = mock_art

        result = cleanup_artworks(_profile(), keep=2, order="newest_first")

        assert result.deleted == ["MY_F0003", "MY_F0004"]
        assert result.kept == 2

    @patch("frameart.tv.cleanup.list_art")
    def test_nothing_to_delete(self, mock_list):
        """When under the limit, nothing is deleted."""
        mock_list.return_value = [_art("MY_F0001"), _art("MY_F0002")]

        result = cleanup_artworks(_profile(), keep=5)

        assert result.deleted == []
        assert result.kept == 2

    @patch("frameart.tv.cleanup._connect")
    @patch("frameart.tv.cleanup.list_art")
    def test_delete_all(self, mock_list, mock_conn):
        """delete_all=True removes all user uploads."""
        mock_list.return_value = [_art("MY_F0001"), _art("MY_F0002"), _art("SAM-S1234")]
        mock_art = MagicMock()
        mock_conn.return_value.art.return_value = mock_art

        result = cleanup_artworks(_profile(), delete_all=True)

        assert result.deleted == ["MY_F0001", "MY_F0002"]
        assert result.kept == 0

    @patch("frameart.tv.cleanup._connect")
    @patch("frameart.tv.cleanup.list_art")
    def test_skips_samsung_store_art(self, mock_list, mock_conn):
        """Samsung Art Store items are never deleted."""
        mock_list.return_value = [
            _art("SAM-S0001"),
            _art("SAM-S0002"),
            _art("MY_F0001"),
        ]
        mock_art = MagicMock()
        mock_conn.return_value.art.return_value = mock_art

        result = cleanup_artworks(_profile(), delete_all=True)

        # Only the MY_ item is deleted
        assert result.deleted == ["MY_F0001"]

    @patch("frameart.tv.cleanup._connect")
    @patch("frameart.tv.cleanup.list_art")
    def test_protects_favourites_by_default(self, mock_list, mock_conn):
        """Favourited artworks are not deleted when include_favourites=False."""
        mock_list.return_value = [
            _art("MY_F0001"),
            _art("MY_F0002", is_favourite=True),
            _art("MY_F0003"),
            _art("MY_F0004"),
        ]
        mock_art = MagicMock()
        mock_conn.return_value.art.return_value = mock_art

        result = cleanup_artworks(
            _profile(), keep=2, order="oldest_first", include_favourites=False,
        )

        # MY_F0002 is favourite — protected. Of the 3 non-favs, delete oldest 1
        assert "MY_F0002" not in result.deleted
        assert result.deleted == ["MY_F0001"]
        assert result.skipped_favourites == 1

    @patch("frameart.tv.cleanup._connect")
    @patch("frameart.tv.cleanup.list_art")
    def test_includes_favourites_when_requested(self, mock_list, mock_conn):
        """Favourites are deleted when include_favourites=True."""
        mock_list.return_value = [
            _art("MY_F0001"),
            _art("MY_F0002", is_favourite=True),
            _art("MY_F0003"),
        ]
        mock_art = MagicMock()
        mock_conn.return_value.art.return_value = mock_art

        result = cleanup_artworks(
            _profile(), keep=1, order="oldest_first", include_favourites=True,
        )

        assert result.deleted == ["MY_F0001", "MY_F0002"]
        assert result.skipped_favourites == 0

    def test_invalid_order(self):
        result = cleanup_artworks(_profile(), order="random")
        assert result.error is not None
        assert "Invalid order" in result.error

    @patch("frameart.tv.cleanup.list_art", side_effect=RuntimeError("TV offline"))
    def test_list_art_failure(self, mock_list):
        result = cleanup_artworks(_profile())
        assert result.error is not None
        assert "TV offline" in result.error
