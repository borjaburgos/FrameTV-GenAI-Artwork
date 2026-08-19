"""Helpers for keeping credentials and signed URLs out of persistent logs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_AUTHORIZATION_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)([^,;\s]+(?:\s+[^,;\s]+)?)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"signature|sig|key)\s*=\s*([^\s&,;]+)"
)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}"


def _safe_url(raw_url: str) -> str:
    trailing = ""
    while raw_url and raw_url[-1] in _TRAILING_URL_PUNCTUATION:
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname or ""
        if not hostname:
            return "[redacted URL]" + trailing
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        safe = urlunsplit((parsed.scheme, hostname + port, parsed.path, "", ""))
        return safe + trailing
    except ValueError:
        return "[redacted URL]" + trailing


def sanitize_log_value(value: object, *, secrets: Iterable[str] = ()) -> str:
    """Return useful error text with credentials and URL queries removed."""
    sanitized = str(value)
    for secret in sorted(
        {item for item in secrets if isinstance(item, str) and len(item) >= 4},
        key=len,
        reverse=True,
    ):
        sanitized = sanitized.replace(secret, "[redacted]")
    sanitized = _URL_RE.sub(lambda match: _safe_url(match.group(0)), sanitized)
    sanitized = _BEARER_RE.sub("Bearer [redacted]", sanitized)
    sanitized = _AUTHORIZATION_RE.sub(r"\1[redacted]", sanitized)
    sanitized = _SECRET_ASSIGNMENT_RE.sub(r"\1=[redacted]", sanitized)
    return sanitized


def safe_exception_message(
    exc: BaseException,
    *,
    secrets: Iterable[str] = (),
) -> str:
    """Describe an exception without formatting request credentials into logs."""
    response = getattr(exc, "response", None)
    request = getattr(exc, "request", None)
    if request is None and response is not None:
        request = getattr(response, "request", None)

    details = [type(exc).__name__]
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        details.append(f"status={status_code}")
    if request is not None:
        method = getattr(request, "method", None)
        if method:
            details.append(str(method).upper())
        url = getattr(request, "url", None)
        if url:
            details.append(_safe_url(str(url)))

    if len(details) > 1:
        return sanitize_log_value(" ".join(details), secrets=secrets)

    message = sanitize_log_value(exc, secrets=secrets).strip()
    return message or type(exc).__name__
