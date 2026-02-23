"""Meural canvas discovery via the local /remote/identify/ endpoint.

Unlike Samsung TVs, Meural canvases do not advertise via SSDP.
Discovery requires either a known IP or a network scan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 3  # seconds per probe


@dataclass
class DiscoveredMeural:
    """A Meural canvas found on the network."""

    ip: str
    name: str = ""
    model: str = ""
    orientation: str = ""


def probe(ip: str, port: int = 80, timeout: float = DEFAULT_TIMEOUT) -> DiscoveredMeural | None:
    """Probe a single IP to see if it hosts a Meural local API.

    Returns a DiscoveredMeural if the device responds to ``/remote/identify/``,
    or None if it does not.
    """
    url = f"http://{ip}:{port}/remote/identify/"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug("Probe %s — not a Meural: %s", ip, e)
        return None

    if data.get("status") != "pass":
        logger.debug("Probe %s — unexpected status: %s", ip, data.get("status"))
        return None

    info = data.get("response", {})
    return DiscoveredMeural(
        ip=ip,
        name=str(info.get("alias", "")),
        model=str(info.get("model", "")),
        orientation=str(info.get("orientation", "")),
    )


def discover_subnet(
    subnet_prefix: str,
    port: int = 80,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[DiscoveredMeural]:
    """Scan a /24 subnet for Meural canvases.

    Parameters
    ----------
    subnet_prefix:
        The first three octets, e.g. ``"192.168.1"``.
    port:
        Port to probe (default 80).
    timeout:
        Per-host connection timeout.

    Returns
    -------
    List of discovered Meural devices.
    """
    import concurrent.futures

    logger.info("Scanning %s.0/24 for Meural canvases...", subnet_prefix)
    ips = [f"{subnet_prefix}.{i}" for i in range(1, 255)]
    found: list[DiscoveredMeural] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(probe, ip, port, timeout): ip for ip in ips}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is not None:
                found.append(result)

    logger.info("Found %d Meural canvas(es) on %s.0/24", len(found), subnet_prefix)
    return sorted(found, key=lambda m: m.ip)
