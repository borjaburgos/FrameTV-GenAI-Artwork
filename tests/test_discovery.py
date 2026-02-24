"""Tests for TV auto-discovery via SSDP."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from frameart.tv.discovery import (
    _SEARCH_TARGETS,
    _SEND_COUNT,
    DiscoveredTV,
    _query_device_info,
    _ssdp_search,
    discover,
)


class TestSSDPSearch:
    @patch("frameart.tv.discovery.time.sleep")
    @patch("frameart.tv.discovery.socket.socket")
    def test_parses_location_header(self, mock_socket_cls, _mock_sleep):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        # Simulate one SSDP response then timeout
        ssdp_response = (
            b"HTTP/1.1 200 OK\r\n"
            b"LOCATION: http://192.168.1.50:9197/dmr\r\n"
            b"ST: urn:samsung.com:device:RemoteControlReceiver:1\r\n"
            b"\r\n"
        )

        mock_sock.recvfrom.side_effect = [
            (ssdp_response, ("192.168.1.50", 1900)),
            TimeoutError("done"),
        ]

        ips = _ssdp_search(timeout=1)
        assert ips == ["192.168.1.50"]

        # Should bind and set multicast TTL
        mock_sock.bind.assert_called_once_with(("", 0))
        mock_sock.setsockopt.assert_any_call(
            __import__("socket").SOL_SOCKET,
            __import__("socket").SO_REUSEADDR,
            1,
        )

        # Should send packets for each search target, repeated _SEND_COUNT times
        expected_sends = len(_SEARCH_TARGETS) * _SEND_COUNT
        assert mock_sock.sendto.call_count == expected_sends

    @patch("frameart.tv.discovery.time.sleep")
    @patch("frameart.tv.discovery.socket.socket")
    def test_no_responses(self, mock_socket_cls, _mock_sleep):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        mock_sock.recvfrom.side_effect = TimeoutError("done")

        ips = _ssdp_search(timeout=1)
        assert ips == []

    @patch("frameart.tv.discovery.time.sleep")
    @patch("frameart.tv.discovery.socket.socket")
    def test_deduplicates_ips(self, mock_socket_cls, _mock_sleep):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        resp = b"HTTP/1.1 200 OK\r\nLOCATION: http://10.0.0.5:9197/dmr\r\n\r\n"

        mock_sock.recvfrom.side_effect = [
            (resp, ("10.0.0.5", 1900)),
            (resp, ("10.0.0.5", 1900)),
            TimeoutError("done"),
        ]

        ips = _ssdp_search(timeout=1)
        assert ips == ["10.0.0.5"]

    @patch("frameart.tv.discovery.time.sleep")
    @patch("frameart.tv.discovery.socket.socket")
    def test_fallback_to_source_address(self, mock_socket_cls, _mock_sleep):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        # Response without a LOCATION header — should fall back to addr[0]
        resp = b"HTTP/1.1 200 OK\r\nST: urn:samsung.com:device:RemoteControlReceiver:1\r\n\r\n"

        mock_sock.recvfrom.side_effect = [
            (resp, ("10.0.0.7", 1900)),
            TimeoutError("done"),
        ]

        ips = _ssdp_search(timeout=1)
        assert ips == ["10.0.0.7"]


class TestQueryDeviceInfo:
    @patch("frameart.tv.discovery.httpx.get")
    def test_frame_tv(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "device": {
                "name": "Living Room",
                "modelName": "QN55LS03",
                "FrameTVSupport": "true",
            },
            "isSupport": '{"FrameTVSupport":"true"}',
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        tv = _query_device_info("192.168.1.50")
        assert tv is not None
        assert tv.ip == "192.168.1.50"
        assert tv.name == "Living Room"
        assert tv.model == "QN55LS03"
        assert tv.frame_tv is True

    @patch("frameart.tv.discovery.httpx.get")
    def test_non_frame_tv(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "device": {"name": "Bedroom", "modelName": "UN50TU8000"},
            "isSupport": "{}",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        tv = _query_device_info("192.168.1.51")
        assert tv is not None
        assert tv.frame_tv is False

    @patch("frameart.tv.discovery.httpx.get")
    def test_unreachable(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")

        tv = _query_device_info("192.168.1.99")
        assert tv is None


class TestDiscover:
    @patch("frameart.tv.discovery._query_device_info")
    @patch("frameart.tv.discovery._ssdp_search")
    def test_returns_all(self, mock_search, mock_query):
        mock_search.return_value = ["10.0.0.1", "10.0.0.2"]
        mock_query.side_effect = [
            DiscoveredTV(ip="10.0.0.1", name="TV1", model="Frame55", frame_tv=True),
            DiscoveredTV(ip="10.0.0.2", name="TV2", model="TU8000", frame_tv=False),
        ]

        tvs = discover()
        assert len(tvs) == 2
        assert tvs[0].frame_tv is True
        assert tvs[1].frame_tv is False

    @patch("frameart.tv.discovery._query_device_info")
    @patch("frameart.tv.discovery._ssdp_search")
    def test_frame_only_filter(self, mock_search, mock_query):
        mock_search.return_value = ["10.0.0.1", "10.0.0.2"]
        mock_query.side_effect = [
            DiscoveredTV(ip="10.0.0.1", name="TV1", model="Frame55", frame_tv=True),
            DiscoveredTV(ip="10.0.0.2", name="TV2", model="TU8000", frame_tv=False),
        ]

        tvs = discover(frame_only=True)
        assert len(tvs) == 1
        assert tvs[0].ip == "10.0.0.1"

    @patch("frameart.tv.discovery._query_device_info")
    @patch("frameart.tv.discovery._ssdp_search")
    def test_skips_unreachable(self, mock_search, mock_query):
        mock_search.return_value = ["10.0.0.1"]
        mock_query.return_value = None

        tvs = discover()
        assert tvs == []
