#!/usr/bin/env python3
"""Run a read-only Frame TV check, with an optional real artwork display test."""

from __future__ import annotations

import argparse
import json
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request_json(base_url, path, *, token=None, method="GET", payload=None, insecure=False):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    context = ssl._create_unverified_context() if insecure else None
    request = Request(base_url.rstrip("/") + path, data=body, headers=headers, method=method)
    with urlopen(request, timeout=30, context=context) as response:
        return response.status, json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="FrameArt base URL")
    parser.add_argument("--token", help="Admin or automation API token")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tv", help="Persistent TV profile ID")
    target.add_argument("--tv-ip", help="Private TV IPv4 address")
    parser.add_argument(
        "--apply-job",
        help="Optional library job ID to display; omit for a read-only check",
    )
    parser.add_argument("--insecure", action="store_true", help="Skip TLS certificate validation")
    args = parser.parse_args()
    target_payload = {"tv": args.tv} if args.tv else {"tv_ip": args.tv_ip}
    target_query = urlencode(target_payload)

    try:
        _status, readiness = request_json(
            args.url,
            "/health/ready",
            token=args.token,
            insecure=args.insecure,
        )
        if readiness.get("status") != "ok":
            print(json.dumps({"step": "readiness", "result": readiness}, indent=2))
            return 1

        _status, tv_status = request_json(
            args.url,
            "/tv/status?" + target_query,
            token=args.token,
            insecure=args.insecure,
        )
        report = {"readiness": readiness, "tv_status": tv_status}
        if not tv_status.get("reachable") or not tv_status.get("art_mode_supported"):
            print(json.dumps(report, indent=2))
            return 1

        if args.apply_job:
            _status, applied = request_json(
                args.url,
                f"/jobs/{args.apply_job}/apply",
                token=args.token,
                method="POST",
                payload={**target_payload, "matte": "none"},
                insecure=args.insecure,
            )
            report["apply"] = applied
            if applied.get("error") or not applied.get("tv_switched"):
                print(json.dumps(report, indent=2))
                return 1

        print(json.dumps(report, indent=2))
        return 0
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
    except (URLError, TimeoutError, ValueError) as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
