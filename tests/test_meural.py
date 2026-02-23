"""Tests for Meural canvas local API controller and discovery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from frameart.config import MeuralProfile
from frameart.meural.controller import (
    _prepare_image,
    change_gallery,
    change_item,
    display_image,
    get_status,
    list_galleries,
    list_gallery_items,
    next_image,
    previous_image,
    reset_brightness,
    set_brightness,
    set_orientation,
    sleep,
    toggle_info_card,
    wake,
)
from frameart.meural.discovery import probe


def _profile(**overrides) -> MeuralProfile:
    defaults = {"ip": "192.168.1.50"}
    defaults.update(overrides)
    return MeuralProfile(**defaults)


# ---------------------------------------------------------------------------
# _prepare_image
# ---------------------------------------------------------------------------


class TestPrepareImage:
    def test_converts_png_to_jpeg(self):
        import io

        from PIL import Image

        img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        jpeg_bytes, content_type = _prepare_image(png_bytes, "vertical")
        assert content_type == "image/jpeg"
        assert jpeg_bytes[:2] == b"\xff\xd8"  # JPEG magic bytes

    def test_converts_jpeg_to_jpeg(self):
        import io

        from PIL import Image

        img = Image.new("RGB", (100, 100), (0, 255, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        jpg_bytes = buf.getvalue()

        result_bytes, content_type = _prepare_image(jpg_bytes, "horizontal")
        assert content_type == "image/jpeg"
        assert result_bytes[:2] == b"\xff\xd8"


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    @patch("frameart.meural.controller._get")
    def test_reachable(self, mock_get):
        mock_get.side_effect = [
            # identify
            {"status": "pass", "response": {
                "alias": "Office Canvas",
                "model": "MC327",
                "orientation": "vertical",
            }},
            # sleep check
            {"status": "pass", "response": "running"},
            # gallery status
            {"status": "pass", "response": {
                "current_gallery": "5",
                "current_item": "42",
            }},
        ]
        status = get_status(_profile())
        assert status.reachable is True
        assert status.sleeping is False
        assert status.device_name == "Office Canvas"
        assert status.device_model == "MC327"
        assert status.orientation == "vertical"
        assert status.current_gallery == "5"
        assert status.current_item == "42"

    @patch("frameart.meural.controller._get")
    def test_sleeping(self, mock_get):
        mock_get.side_effect = [
            {"status": "pass", "response": {"alias": "X", "model": "Y"}},
            {"status": "pass", "response": "suspended"},
            {"status": "pass", "response": {}},
        ]
        status = get_status(_profile())
        assert status.sleeping is True

    @patch("frameart.meural.controller._get", side_effect=ConnectionError("refused"))
    def test_unreachable(self, mock_get):
        status = get_status(_profile())
        assert status.reachable is False
        assert "refused" in status.error


# ---------------------------------------------------------------------------
# display_image
# ---------------------------------------------------------------------------


class TestDisplayImage:
    @patch("frameart.meural.controller._pause_slideshow")
    @patch("frameart.meural.controller._retry")
    def test_success_duration_zero_pauses(self, mock_retry, mock_pause):
        mock_retry.return_value = {"status": "pass", "response": "ok"}

        import io

        from PIL import Image

        img = Image.new("RGB", (100, 100), (0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        img_bytes = buf.getvalue()

        result = display_image(_profile(), img_bytes, duration=0)
        assert result.success is True
        mock_pause.assert_called_once()

    @patch("frameart.meural.controller._pause_slideshow")
    @patch("frameart.meural.controller._retry")
    def test_success_nonzero_duration_no_pause(self, mock_retry, mock_pause):
        mock_retry.return_value = {"status": "pass", "response": "ok"}

        import io

        from PIL import Image

        img = Image.new("RGB", (100, 100), (0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        img_bytes = buf.getvalue()

        result = display_image(_profile(), img_bytes, duration=60)
        assert result.success is True
        mock_pause.assert_not_called()

    @patch("frameart.meural.controller._retry")
    def test_device_returns_fail_status(self, mock_retry):
        mock_retry.return_value = {"status": "fail", "response": "bad image"}

        import io

        from PIL import Image

        img = Image.new("RGB", (100, 100))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")

        result = display_image(_profile(), buf.getvalue())
        assert result.success is False
        assert "fail" in result.error

    @patch("frameart.meural.controller._retry", side_effect=RuntimeError("timeout"))
    def test_connection_error(self, mock_retry):
        import io

        from PIL import Image

        img = Image.new("RGB", (100, 100))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")

        result = display_image(_profile(), buf.getvalue())
        assert result.success is False
        assert "timeout" in result.error


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------


class TestOrientation:
    @patch("frameart.meural.controller._get")
    def test_set_portrait(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert set_orientation(_profile(), "portrait") is True

    @patch("frameart.meural.controller._get")
    def test_set_landscape(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert set_orientation(_profile(), "landscape") is True

    def test_invalid_orientation(self):
        import pytest
        with pytest.raises(ValueError, match="Invalid orientation"):
            set_orientation(_profile(), "diagonal")

    @patch("frameart.meural.controller._get", side_effect=ConnectionError)
    def test_failure(self, mock_get):
        assert set_orientation(_profile(), "portrait") is False


# ---------------------------------------------------------------------------
# Brightness
# ---------------------------------------------------------------------------


class TestBrightness:
    @patch("frameart.meural.controller._get")
    def test_set_brightness(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert set_brightness(_profile(), 75) is True

    @patch("frameart.meural.controller._get")
    def test_clamps_value(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert set_brightness(_profile(), 150) is True
        url = mock_get.call_args[0][1]
        assert "/100/" in url

    @patch("frameart.meural.controller._get")
    def test_reset_brightness(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert reset_brightness(_profile()) is True


# ---------------------------------------------------------------------------
# Sleep / Wake
# ---------------------------------------------------------------------------


class TestSleepWake:
    @patch("frameart.meural.controller._get")
    def test_sleep(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert sleep(_profile()) is True

    @patch("frameart.meural.controller._get")
    def test_wake(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert wake(_profile()) is True

    @patch("frameart.meural.controller._get", side_effect=ConnectionError)
    def test_sleep_failure(self, mock_get):
        assert sleep(_profile()) is False


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


class TestNavigation:
    @patch("frameart.meural.controller._get")
    def test_next(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert next_image(_profile()) is True

    @patch("frameart.meural.controller._get")
    def test_previous(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert previous_image(_profile()) is True

    @patch("frameart.meural.controller._get")
    def test_toggle_info(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert toggle_info_card(_profile()) is True


# ---------------------------------------------------------------------------
# Galleries
# ---------------------------------------------------------------------------


class TestGalleries:
    @patch("frameart.meural.controller._get")
    def test_list_galleries(self, mock_get):
        mock_get.return_value = {
            "status": "pass",
            "response": [
                {"id": 1, "name": "Favorites", "item_count": 5},
                {"id": 2, "name": "Landscapes", "item_count": 12},
            ],
        }
        galleries = list_galleries(_profile())
        assert len(galleries) == 2
        assert galleries[0].id == "1"
        assert galleries[0].name == "Favorites"
        assert galleries[1].item_count == 12

    @patch("frameart.meural.controller._get")
    def test_change_gallery(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert change_gallery(_profile(), "3") is True

    @patch("frameart.meural.controller._get")
    def test_change_item(self, mock_get):
        mock_get.return_value = {"status": "pass"}
        assert change_item(_profile(), "42") is True

    @patch("frameart.meural.controller._get")
    def test_list_gallery_items(self, mock_get):
        mock_get.return_value = {
            "status": "pass",
            "response": [
                {"id": 10, "name": "Sunset"},
                {"id": 11, "name": "Mountains"},
            ],
        }
        items = list_gallery_items(_profile(), "1")
        assert len(items) == 2


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestProbe:
    @patch("frameart.meural.discovery.httpx.get")
    def test_meural_found(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "pass",
            "response": {
                "alias": "Living Room",
                "model": "MC327",
                "orientation": "portrait",
            },
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = probe("192.168.1.50")
        assert result is not None
        assert result.ip == "192.168.1.50"
        assert result.name == "Living Room"
        assert result.model == "MC327"

    @patch("frameart.meural.discovery.httpx.get", side_effect=ConnectionError)
    def test_not_a_meural(self, mock_get):
        result = probe("192.168.1.1")
        assert result is None

    @patch("frameart.meural.discovery.httpx.get")
    def test_non_pass_status(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "fail", "response": "error"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = probe("192.168.1.50")
        assert result is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestMeuralProfile:
    def test_defaults(self):
        profile = MeuralProfile(ip="192.168.1.50")
        assert profile.port == 80
        assert profile.orientation == "vertical"
        assert profile.name == ""

    def test_custom(self):
        profile = MeuralProfile(
            ip="10.0.0.5", port=8080, orientation="horizontal", name="Office",
        )
        assert profile.ip == "10.0.0.5"
        assert profile.port == 8080
        assert profile.orientation == "horizontal"
        assert profile.name == "Office"
