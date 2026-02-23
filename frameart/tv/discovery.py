"""Samsung Frame TV auto-discovery via SSDP (UPnP).

Sends an M-SEARCH multicast to find Samsung TVs on the local network,
then queries each one's REST API to check for Frame TV support.
"""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 3  # seconds to wait for responses

# Samsung TVs respond to this search target
SAMSUNG_ST = "urn:samsung.com:device:RemoteControlReceiver:1"

_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    f"MX: {SSDP_MX}\r\n"
    f"ST: {SAMSUNG_ST}\r\n"
    "\r\n"
)

# Regex to extract IP from LOCATION header like http://192.168.1.50:9197/...
_LOCATION_RE = re.compile(r"https?://(\d+\.\d+\.\d+\.\d+)")


@dataclass
class DiscoveredTV:
    """A Samsung TV found on the network."""

    ip: str
    name: str = "Unknown"
    model: str = "Unknown"
    frame_tv: bool = False


def _ssdp_search(timeout: float = SSDP_MX + 1) -> list[str]:
    """Send SSDP M-SEARCH and return unique IPs that respond."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)

    ips: set[str] = set()
    try:
        sock.sendto(_MSEARCH.encode(), (SSDP_ADDR, SSDP_PORT))
        logger.debug("Sent SSDP M-SEARCH for %s", SAMSUNG_ST)

        while True:
            try:
                data, addr = sock.recvfrom(4096)
                response = data.decode(errors="replace")
                # Extract IP from LOCATION header
                match = _LOCATION_RE.search(response)
                if match:
                    ips.add(match.group(1))
                else:
                    # Fall back to source address
                    ips.add(addr[0])
            except TimeoutError:
                break
    finally:
        sock.close()

    logger.info("SSDP found %d Samsung device(s): %s", len(ips), ", ".join(sorted(ips)))
    return sorted(ips)


def _query_device_info(ip: str, timeout: float = 5.0) -> DiscoveredTV | None:
    """Query the Samsung REST API for device info."""
    url = f"http://{ip}:8001/api/v2/"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug("Could not query %s: %s", ip, e)
        return None

    device = data.get("device", {})
    is_support_str = data.get("isSupport", "{}")
    frame_tv = (
        device.get("FrameTVSupport") == "true"
        or '"FrameTVSupport":"true"' in is_support_str
    )

    return DiscoveredTV(
        ip=ip,
        name=device.get("name", "Unknown"),
        model=device.get("modelName", "Unknown"),
        frame_tv=frame_tv,
    )


def discover(timeout: float = SSDP_MX + 1, frame_only: bool = False) -> list[DiscoveredTV]:
    """Discover Samsung TVs on the local network.

    Parameters
    ----------
    timeout:
        How long to wait for SSDP responses.
    frame_only:
        If True, only return TVs that support Frame/Art mode.
    """
    ips = _ssdp_search(timeout=timeout)
    tvs: list[DiscoveredTV] = []

    for ip in ips:
        tv = _query_device_info(ip)
        if tv is None:
            continue
        if frame_only and not tv.frame_tv:
            continue
        tvs.append(tv)

    logger.info(
        "Discovered %d TV(s)%s", len(tvs), " (frame-only filter)" if frame_only else "",
    )
    return tvs
