"""Small, deliberately read-only ResourceSpace API client.

The signing/envelope protocol follows ResourceSpace's official POST example.
No mutating API functions are exposed and no signed URL is sent to a browser.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlencode, urljoin, urlsplit

import httpx2 as httpx

CATALOG_LIMIT = 1000
# Kept for integrations using the pilot's original public constant.
PILOT_LIMIT = CATALOG_LIMIT
COLLECTION_PAGE_SIZE = 100
MAX_API_BYTES = 2 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024
MAX_RENDITION_BYTES = 32 * 1024 * 1024
READ_FUNCTIONS = frozenset({"do_search", "get_resource_field_data", "get_resource_path"})
_FRAMEART_FIELDS = (
    "frameart_id",
    "frameart_metadata",
    "frameart_artist",
    "frameart_artist_bio",
    "frameart_date",
    "frameart_medium",
    "frameart_license",
    "frameart_source_url",
)
_FIELD_ALIASES = {name.replace("_", ""): name for name in _FRAMEART_FIELDS}
_preview_request_active: ContextVar[bool] = ContextVar(
    "resourcespace_preview_request", default=False
)


def canonical_field_name(name: str) -> str:
    """Map only our known fields sanitized by ResourceSpace 11 field creation.

    Keep our portable schema stable without interpreting unrelated catalog
    fields. Legacy installations retaining the underscores also work.
    """
    return _FIELD_ALIASES.get(name, name)


class _PreviewRequestLogFilter(logging.Filter):
    """Redact HTTP transport details only inside this request's execution context."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _preview_request_active.get():
            # ResourceSpace temporarily authorizes previews through the URL.
            # Redact the entire message, not only its query: path components,
            # response headers and exception text can carry credentials too.
            record.msg = "ResourceSpace preview HTTP details redacted"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


# Filters attached to parent loggers do not run for propagated child records.
# Install one persistent filter on each HTTPX/HTTPCore transport logger, without
# changing logging levels or handlers. A ContextVar keeps concurrent ordinary
# requests untouched; never remove a filter while another preview is in flight.
_preview_log_filter = _PreviewRequestLogFilter()
for _logger_name in (
    "httpx2",
    "httpcore2",
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
    "httpcore2.proxy",
    "httpcore2.socks",
):
    logging.getLogger(_logger_name).addFilter(_preview_log_filter)


@contextmanager
def _redacted_preview_logs() -> Iterator[None]:
    token = _preview_request_active.set(True)
    try:
        yield
    finally:
        _preview_request_active.reset(token)


class ResourceSpaceError(RuntimeError):
    """A safe, non-sensitive integration error suitable for the API."""


def validated_base_url(value: str) -> str:
    """Accept an explicitly configured server, never credentials in a URL."""
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ResourceSpaceError("ResourceSpace URL must be an HTTP(S) server URL.") from None
    return value.strip().rstrip("/")


def signed_post_data(user: str, key: str, function: str, **parameters: object) -> dict[str, str]:
    """Sign the exact encoded inner query, then wrap it in the POST envelope."""
    if function not in READ_FUNCTIONS:
        raise ResourceSpaceError("Only read-only ResourceSpace functions are permitted.")
    values = {"user": user, "function": function}
    for name, value in parameters.items():
        values[name] = str(int(value)) if isinstance(value, bool) else str(value)
    query = urlencode(values)
    signature = hashlib.sha256((key + query).encode("utf-8")).hexdigest()
    return {"query": query + "&sign=" + signature, "sign": signature, "user": user}


def bounded_search(call: Callable, search: str, *, maximum: int = CATALOG_LIMIT) -> list[dict]:
    """Read every ordered search page, refusing partial or changing results.

    ResourceSpace 10.3+ returns ``{total, data}`` for an offset,count
    fetchrows value. Requiring that envelope detects silent truncation;
    ordered unique references detect ignored offsets and repeated pages.
    """
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 1 <= maximum <= CATALOG_LIMIT
    ):
        raise ResourceSpaceError("ResourceSpace search limit is invalid.")
    resources: list[dict] = []
    total: int | None = None
    last_ref = 0
    while total is None or len(resources) < total:
        result = call(
            "do_search",
            search=search,
            archive=0,
            order_by="resourceid",
            sort="asc",
            fetchrows=f"{len(resources)},{COLLECTION_PAGE_SIZE}",
        )
        if not isinstance(result, dict):
            raise ResourceSpaceError("ResourceSpace returned an invalid collection page.")
        reported = _decimal_integer(result.get("total"))
        if reported is None:
            raise ResourceSpaceError("ResourceSpace returned an invalid collection total.")
        if reported > maximum:
            raise ResourceSpaceError("ResourceSpace collection exceeds the catalog size limit.")
        if total is not None and reported != total:
            raise ResourceSpaceError("ResourceSpace collection changed during pagination.")
        total = reported
        rows = result.get("data")
        expected = min(COLLECTION_PAGE_SIZE, total - len(resources))
        if not isinstance(rows, list) or len(rows) != expected:
            raise ResourceSpaceError("ResourceSpace returned an incomplete collection page.")
        for row in rows:
            ref = _decimal_integer(row.get("ref")) if isinstance(row, dict) else None
            if ref is None or ref <= last_ref:
                raise ResourceSpaceError(
                    "ResourceSpace returned duplicate or invalid artwork references."
                )
            last_ref = ref
        resources.extend(rows)
    return resources


def _decimal_integer(value: object) -> int | None:
    """Accept bounded JSON integers/decimal strings, never floats or booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    encoded = str(value)
    if not 1 <= len(encoded) <= 20 or not encoded.isascii() or not encoded.isdecimal():
        return None
    return int(encoded)


class ResourceSpaceClient:
    """Read a bounded collection and its existing image files with hard limits."""

    def __init__(self, base_url: str, user: str, key: str, *, timeout: float = 10) -> None:
        self.base_url = validated_base_url(base_url)
        self.user = user
        self._key = key
        self.timeout = timeout

    @staticmethod
    def _read_response(response, maximum: int) -> bytes:
        response.raise_for_status()
        payload = bytearray()
        for chunk in response.iter_bytes():
            if len(payload) + len(chunk) > maximum:
                raise ResourceSpaceError("ResourceSpace response exceeded its size limit.")
            payload.extend(chunk)
        return bytes(payload)

    def call(self, function: str, **parameters: object):
        data = signed_post_data(self.user, self._key, function, **parameters)
        try:
            with (
                httpx.Client(
                    timeout=self.timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("POST", self.base_url + "/api/", data=data) as response,
            ):
                payload = self._read_response(response, MAX_API_BYTES)
            result = json.loads(payload)
        except ResourceSpaceError:
            raise
        except Exception:
            # Upstream bodies, URLs and exceptions may contain credentials.
            raise ResourceSpaceError(
                "Could not read ResourceSpace. Check its connection and reader permissions."
            ) from None
        if function == "get_resource_path" and (result is False or result is None):
            return None
        if result is False or result is None or (isinstance(result, dict) and "error" in result):
            raise ResourceSpaceError(
                "ResourceSpace rejected the read request. Check reader permissions."
            )
        return result

    def collection_resources(self, collection_id: int) -> list[dict]:
        return bounded_search(self.call, f"!collection{collection_id}")

    def resource_fields(self, ref: int) -> dict[str, str]:
        result = self.call("get_resource_field_data", resource=ref)
        if not isinstance(result, list):
            raise ResourceSpaceError("ResourceSpace returned invalid artwork metadata.")
        fields: dict[str, str] = {}
        for field in result:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            name = canonical_field_name(str(field["name"]))
            value = str(field.get("value") or "")
            if name in fields and fields[name] != value:
                raise ResourceSpaceError(
                    "ResourceSpace returned conflicting artwork metadata fields."
                )
            fields[name] = value
        return fields

    def thumbnail(self, ref: int, extension: str = "jpg") -> bytes | None:
        if extension.lower() not in {"jpg", "jpeg", "png", "webp"}:
            extension = "jpg"
        result = None
        for size in ("pre", "thm"):
            result = self.call(
                "get_resource_path",
                ref=ref,
                size=size,
                generate=0,
                extension=extension,
                write_metadata=False,
            )
            if result is not None and result is not False:
                break
        if result is None or result is False:
            return None
        return self._download_image(result, MAX_THUMBNAIL_BYTES, "preview")

    def rendition(self, ref: int) -> bytes:
        """Read an existing managed JPEG rendition; never generate or alter it."""
        if isinstance(ref, bool) or not isinstance(ref, int) or ref < 1:
            raise ResourceSpaceError("ResourceSpace artwork reference must be a positive integer.")
        result = self.call(
            "get_resource_path",
            ref=ref,
            size="",
            generate=0,
            extension="jpg",
            write_metadata=False,
        )
        if result is None or result is False:
            raise ResourceSpaceError("ResourceSpace artwork rendition is unavailable.")
        return self._download_image(result, MAX_RENDITION_BYTES, "rendition")

    def _download_image(self, result: object, maximum: int, label: str) -> bytes:
        """Keep every temporarily authorized image URL on the configured origin."""
        if not isinstance(result, str) or not result:
            raise ResourceSpaceError(f"ResourceSpace returned an invalid {label} location.")
        try:
            location = urljoin(self.base_url + "/", result)
            origin, target = urlsplit(self.base_url), urlsplit(location)
            origin_port = origin.port or (443 if origin.scheme == "https" else 80)
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            raise ResourceSpaceError(
                f"ResourceSpace returned an invalid {label} location."
            ) from None
        if (
            (origin.scheme, origin.hostname, origin_port)
            != (target.scheme, target.hostname, target_port)
            or target.username is not None
            or target.password is not None
            or target.fragment
        ):
            raise ResourceSpaceError(f"ResourceSpace {label} must use the configured server.")
        try:
            with (
                _redacted_preview_logs(),
                httpx.Client(
                    timeout=self.timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("GET", location) as response,
            ):
                return self._read_response(response, maximum)
        except ResourceSpaceError:
            raise
        except Exception:
            raise ResourceSpaceError(f"Could not read a ResourceSpace {label}.") from None
