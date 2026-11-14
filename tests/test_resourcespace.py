"""Protocol and read-boundary tests for the local ResourceSpace reader."""

from __future__ import annotations

import hashlib
import logging
import threading
from urllib.parse import parse_qs

import httpx2 as httpx
import pytest

from frameart import resourcespace
from frameart.resourcespace import (
    ResourceSpaceClient,
    ResourceSpaceError,
    canonical_field_name,
    signed_post_data,
    validated_base_url,
)


@pytest.fixture
def install_transport(monkeypatch):
    client_type = httpx.Client

    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            resourcespace.httpx,
            "Client",
            lambda **kwargs: client_type(transport=transport, **kwargs),
        )

    return install


def test_signs_exact_encoded_query_and_post_envelope():
    result = signed_post_data("reader@example", "private-key", "do_search", search="!collection3")
    query = "user=reader%40example&function=do_search&search=%21collection3"
    signature = hashlib.sha256(("private-key" + query).encode()).hexdigest()
    assert result == {
        "query": query + "&sign=" + signature,
        "sign": signature,
        "user": "reader@example",
    }
    assert "private-key" not in str(result)


@pytest.mark.parametrize("function", ["upload_file", "delete_resource", "update_field", "login"])
def test_rejects_mutating_api_functions(function):
    with pytest.raises(ResourceSpaceError, match="read-only"):
        signed_post_data("reader", "key", function, resource=1)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:key@catalog.local",
        "https://catalog.local/?key=secret",
        "https://catalog.local/#fragment",
        "https://catalog.local:bad",
        "not a URL",
    ],
)
def test_rejects_unsafe_configured_urls(url):
    with pytest.raises(ResourceSpaceError, match="HTTP"):
        validated_base_url(url)


def test_collection_search_is_bounded_and_posts_no_secret(install_transport):
    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "http://catalog.local/api/"
        assert "private-key" not in request.content.decode()
        outer = parse_qs(request.content.decode())
        inner = parse_qs(outer["query"][0])
        assert inner["search"] == ["!collection9"]
        assert inner["fetchrows"] == ["0,100"]
        assert inner["archive"] == ["0"]
        return httpx.Response(200, json={"total": 1, "data": [{"ref": "7"}]})

    install_transport(handler)
    client = ResourceSpaceClient("http://catalog.local", "reader", "private-key")
    assert client.collection_resources(9) == [{"ref": "7"}]


@pytest.mark.parametrize("count", [0, 20, 100, 365, 1000])
def test_collection_reads_every_ordered_page_without_truncation(install_transport, count):
    offsets = []
    rows = [{"ref": str(ref)} for ref in range(1, count + 1)]

    def handler(request):
        inner = parse_qs(parse_qs(request.content.decode())["query"][0])
        offset, limit = map(int, inner["fetchrows"][0].split(","))
        offsets.append(offset)
        assert limit == 100
        assert inner["order_by"] == ["resourceid"]
        assert inner["sort"] == ["asc"]
        return httpx.Response(
            200, json={"total": str(count), "data": rows[offset : offset + limit]}
        )

    install_transport(handler)
    assert (
        ResourceSpaceClient("http://catalog.local", "reader", "key").collection_resources(9) == rows
    )
    assert offsets == list(range(0, max(count, 1), 100))


@pytest.mark.parametrize(
    "result",
    [
        [],
        {"data": []},
        {"total": True, "data": []},
        {"total": -1, "data": []},
        {"total": "²", "data": []},
        {"total": "9" * 5000, "data": []},
        {"total": 1001, "data": []},
        {"total": 1, "data": []},
        {"total": 0, "data": [{"ref": 1}]},
        {"total": 1, "data": [{"ref": True}]},
        {"total": 1, "data": [{"ref": "²"}]},
        {"total": 1, "data": [{"ref": "9" * 5000}]},
        {"total": 1, "data": [{"ref": 0}]},
        {"total": 2, "data": [{"ref": 1}, {"ref": "1"}]},
        {"total": 2, "data": [{"ref": 2}, {"ref": 1}]},
    ],
)
def test_collection_rejects_invalid_incomplete_or_oversized_pages(install_transport, result):
    install_transport(lambda request: httpx.Response(200, json=result))
    with pytest.raises(ResourceSpaceError):
        ResourceSpaceClient("http://catalog.local", "reader", "key").collection_resources(9)


@pytest.mark.parametrize("failure", ["repeated", "changed_total", "short_page"])
def test_collection_rejects_repeated_or_changing_second_page(install_transport, failure):
    calls = []

    def handler(request):
        calls.append(request)
        rows = [{"ref": ref} for ref in range(1, 101)]
        if len(calls) == 1:
            return httpx.Response(200, json={"total": 200, "data": rows})
        if failure == "changed_total":
            return httpx.Response(200, json={"total": 199, "data": []})
        if failure == "short_page":
            return httpx.Response(200, json={"total": 200, "data": []})
        return httpx.Response(200, json={"total": 200, "data": rows})

    install_transport(handler)
    with pytest.raises(ResourceSpaceError):
        ResourceSpaceClient("http://catalog.local", "reader", "key").collection_resources(9)
    assert len(calls) == 2  # No unbounded loop when an upstream ignores offsets.


def test_upstream_error_bodies_and_urls_are_not_exposed(install_transport):
    def handler(request):
        return httpx.Response(401, text="Secret private-key internal details")

    install_transport(handler)
    with pytest.raises(ResourceSpaceError) as error:
        ResourceSpaceClient("http://catalog.local", "reader", "private-key").collection_resources(9)
    assert "private-key" not in str(error.value)
    assert "catalog.local" not in str(error.value)
    assert "internal details" not in str(error.value)


def test_resource_metadata_fields_use_shortnames(install_transport):
    install_transport(
        lambda request: httpx.Response(
            200,
            json=[
                {"name": "title", "value": "Water Lilies"},
                {"name": "artist", "value": None},
            ],
        )
    )
    result = ResourceSpaceClient("http://catalog.local", "reader", "key").resource_fields(7)
    assert result == {"title": "Water Lilies", "artist": ""}


@pytest.mark.parametrize(
    "canonical",
    [
        "frameart_id",
        "frameart_metadata",
        "frameart_artist",
        "frameart_artist_bio",
        "frameart_date",
        "frameart_medium",
        "frameart_license",
        "frameart_source_url",
    ],
)
def test_only_known_frameart_field_aliases_are_canonicalized(canonical):
    assert canonical_field_name(canonical) == canonical
    assert canonical_field_name(canonical.replace("_", "")) == canonical
    assert canonical_field_name("unrelated_metadata") == "unrelated_metadata"
    assert canonical_field_name("unrelatedmetadata") == "unrelatedmetadata"


def test_conflicting_sanitized_field_aliases_are_rejected(install_transport):
    install_transport(
        lambda request: httpx.Response(
            200,
            json=[
                {"name": "frameartmetadata", "value": "one"},
                {"name": "frameart_metadata", "value": "different"},
            ],
        )
    )
    with pytest.raises(ResourceSpaceError, match="conflicting"):
        ResourceSpaceClient("http://catalog.local", "reader", "key").resource_fields(7)


@pytest.mark.parametrize(
    "location",
    [
        "http://other.local/image.jpg",
        "https://catalog.local/image.jpg",
        "http://catalog.local:1234/image.jpg",
        "http://user:secret@catalog.local/image.jpg",
        "//other.local/image.jpg",
        "file:///etc/passwd",
    ],
)
def test_thumbnail_cannot_fetch_other_origins_or_credentials(install_transport, location):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=location)

    install_transport(handler)
    with pytest.raises(ResourceSpaceError, match="configured server"):
        ResourceSpaceClient("http://catalog.local", "reader", "key").thumbnail(7)
    assert len(calls) == 1


def test_thumbnail_does_not_generate_or_write_original_metadata(install_transport):
    def handler(request):
        if request.method == "POST":
            outer = parse_qs(request.content.decode())
            inner = parse_qs(outer["query"][0])
            assert inner["generate"] == ["0"]
            assert inner["write_metadata"] == ["0"]
            assert inner["size"] == ["pre"]
            return httpx.Response(
                200,
                json="http://catalog.local/filestore/thumb.jpg?access_key=temporary",
            )
        return httpx.Response(200, content=b"preview-bytes")

    install_transport(handler)
    client = ResourceSpaceClient("http://catalog.local", "reader", "key")
    assert client.thumbnail(7) == b"preview-bytes"


def test_missing_preview_is_allowed_without_generating_it(install_transport):
    install_transport(lambda request: httpx.Response(200, json=False))
    assert ResourceSpaceClient("http://catalog.local", "reader", "key").thumbnail(7) is None


@pytest.mark.parametrize("missing", [False, None])
def test_existing_small_thumbnail_is_fallback_only_when_preview_is_missing(
    install_transport,
    missing,
):
    sizes = []
    downloads = []

    def handler(request):
        if request.method == "POST":
            outer = parse_qs(request.content.decode())
            inner = parse_qs(outer["query"][0])
            size = inner["size"][0]
            sizes.append(size)
            assert inner["generate"] == ["0"]
            assert inner["write_metadata"] == ["0"]
            if size == "pre" and missing is None:
                return httpx.Response(200, content=b"null")
            return httpx.Response(
                200,
                json=missing if size == "pre" else "http://catalog.local/existing-thm.jpg",
            )
        downloads.append(request.url.path)
        return httpx.Response(200, content=b"fallback-preview")

    install_transport(handler)
    assert ResourceSpaceClient("http://catalog.local", "reader", "key").thumbnail(7) == (
        b"fallback-preview"
    )
    assert sizes == ["pre", "thm"]
    assert downloads == ["/existing-thm.jpg"]


def test_existing_preview_is_preferred_without_thumbnail_or_original_download(install_transport):
    sizes = []
    downloads = []

    def handler(request):
        if request.method == "POST":
            outer = parse_qs(request.content.decode())
            inner = parse_qs(outer["query"][0])
            sizes.append(inner["size"][0])
            assert inner["generate"] == ["0"]
            assert inner["write_metadata"] == ["0"]
            return httpx.Response(200, json="http://catalog.local/existing-pre.jpg")
        downloads.append(request.url.path)
        return httpx.Response(200, content=b"larger-preview")

    install_transport(handler)
    assert ResourceSpaceClient("http://catalog.local", "reader", "key").thumbnail(7) == (
        b"larger-preview"
    )
    assert sizes == ["pre"]
    assert downloads == ["/existing-pre.jpg"]


def test_thumbnail_redirect_is_not_followed(install_transport):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json="http://catalog.local/thumb.jpg")
        return httpx.Response(302, headers={"location": "http://other.local/private"})

    install_transport(handler)
    with pytest.raises(ResourceSpaceError, match="preview"):
        ResourceSpaceClient("http://catalog.local", "reader", "key").thumbnail(7)


def test_limits_download_bytes_before_caching(install_transport, monkeypatch):
    monkeypatch.setattr(resourcespace, "MAX_THUMBNAIL_BYTES", 5)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json="http://catalog.local/thumb.jpg")
        return httpx.Response(200, content=b"123456")

    install_transport(handler)
    with pytest.raises(ResourceSpaceError, match="size limit"):
        ResourceSpaceClient("http://catalog.local", "reader", "key").thumbnail(7)


def test_rendition_requests_existing_full_jpeg_without_generation_or_metadata_writes(
    install_transport, caplog
):
    caplog.set_level(logging.INFO, logger="httpx2")

    def handler(request):
        if request.method == "POST":
            inner = parse_qs(parse_qs(request.content.decode())["query"][0], keep_blank_values=True)
            assert inner["function"] == ["get_resource_path"]
            assert inner["ref"] == ["7"]
            assert inner["size"] == [""]
            assert inner["extension"] == ["jpg"]
            assert inner["generate"] == ["0"]
            assert inner["write_metadata"] == ["0"]
            return httpx.Response(
                200, json="http://catalog.local/full.jpg?access_key=RENDER-SECRET"
            )
        return httpx.Response(200, content=b"raw-jpeg-bytes")

    install_transport(handler)
    assert (
        ResourceSpaceClient("http://catalog.local", "reader", "key").rendition(7)
        == b"raw-jpeg-bytes"
    )
    assert "RENDER-SECRET" not in caplog.text


@pytest.mark.parametrize(
    "location",
    [
        "https://other.local/full.jpg",
        "//other.local/full.jpg",
        "http://reader:secret@catalog.local/full.jpg",
        "http://catalog.local:8187/full.jpg",
        "http://catalog.local/full.jpg#fragment",
        "file:///etc/passwd",
    ],
)
def test_rendition_rejects_other_origins_credentials_and_fragments(install_transport, location):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=location)

    install_transport(handler)
    with pytest.raises(ResourceSpaceError, match="configured server"):
        ResourceSpaceClient("http://catalog.local", "reader", "key").rendition(7)
    assert len(requests) == 1


def test_rendition_never_follows_redirects(install_transport):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json="http://catalog.local/full.jpg")
        return httpx.Response(302, headers={"Location": "http://other.local/private"})

    install_transport(handler)
    with pytest.raises(ResourceSpaceError, match="rendition"):
        ResourceSpaceClient("http://catalog.local", "reader", "key").rendition(7)
    assert len(requests) == 2


def test_rendition_invalid_ipv6_url_fails_without_exposing_details(install_transport):
    install_transport(lambda request: httpx.Response(200, json="http://[bad?access_key=SECRET"))
    with pytest.raises(ResourceSpaceError, match="invalid rendition location") as error:
        ResourceSpaceClient("http://catalog.local", "reader", "key").rendition(7)
    assert "SECRET" not in str(error.value)


def test_rendition_enforces_streaming_32_mib_limit(install_transport, monkeypatch):
    assert resourcespace.MAX_RENDITION_BYTES == 32 * 1024 * 1024
    monkeypatch.setattr(resourcespace, "MAX_RENDITION_BYTES", 5)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json="http://catalog.local/full.jpg")
        return httpx.Response(200, content=b"123456")

    install_transport(handler)
    with pytest.raises(ResourceSpaceError, match="size limit"):
        ResourceSpaceClient("http://catalog.local", "reader", "key").rendition(7)


@pytest.mark.parametrize("missing", [False, None])
def test_rendition_requires_an_existing_file(install_transport, missing):
    install_transport(
        lambda request: httpx.Response(200, content=b"null" if missing is None else b"false")
    )
    with pytest.raises(ResourceSpaceError, match="unavailable"):
        ResourceSpaceClient("http://catalog.local", "reader", "key").rendition(7)


@pytest.mark.parametrize("ref", [True, 0, -1, "7"])
def test_rendition_rejects_invalid_references_before_network(install_transport, ref):
    def handler(request):
        raise AssertionError("Invalid references must never reach ResourceSpace")

    install_transport(handler)
    with pytest.raises(ResourceSpaceError, match="positive integer"):
        ResourceSpaceClient("http://catalog.local", "reader", "key").rendition(ref)


@pytest.mark.parametrize(
    "location",
    [
        "http://catalog.local/download.php?ref=7&access_key=SYNTHETIC-TEMPORARY-SECRET",
        "http://catalog.local/SYNTHETIC-TEMPORARY-SECRET/thumb.jpg",
    ],
)
def test_preview_authorization_is_redacted_from_http_info_and_transport_debug_logs(
    install_transport,
    caplog,
    location,
):
    caplog.set_level(logging.INFO, logger="httpx2")
    caplog.set_level(logging.DEBUG, logger="httpcore2.http11")

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=location)
        # Real HTTPCore debug tracing includes response headers. Even when its
        # outgoing Request repr omits the URL, Location headers can repeat it.
        logging.getLogger("httpcore2.http11").debug(
            "receive_response_headers.complete return_value=%r",
            [(b"location", location.encode())],
        )
        return httpx.Response(200, content=b"preview")

    install_transport(handler)
    client = ResourceSpaceClient("http://catalog.local", "reader", "private-reader-key")
    assert client.thumbnail(7) == b"preview"
    assert "SYNTHETIC-TEMPORARY-SECRET" not in caplog.text
    assert "private-reader-key" not in caplog.text
    assert "ResourceSpace preview HTTP details redacted" in caplog.text
    # Logging policy for ordinary requests is left intact after the transfer.
    with httpx.Client() as ordinary:
        ordinary.get("http://catalog.local/ordinary-public-request")
    assert "http://catalog.local/ordinary-public-request" in caplog.text


def test_failed_preview_resets_redaction_scope_and_does_not_log_exception_secrets(
    install_transport,
    caplog,
):
    caplog.set_level(logging.INFO, logger="httpx2")
    caplog.set_level(logging.DEBUG, logger="httpcore2.http11")

    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                200, json="http://catalog.local/thumb?access_key=TEMPORARY-SECRET"
            )
        if request.url.path == "/ordinary-public-request":
            return httpx.Response(200)
        try:
            raise RuntimeError("transport failed for access_key=TEMPORARY-SECRET")
        except RuntimeError:
            logging.getLogger("httpcore2.http11").exception("Preview error: %s", request.url)
            raise

    install_transport(handler)
    client = ResourceSpaceClient("http://catalog.local", "reader", "key")
    with pytest.raises(ResourceSpaceError, match="Could not read a ResourceSpace preview"):
        client.thumbnail(7)
    assert "TEMPORARY-SECRET" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    with httpx.Client() as ordinary:
        ordinary.get("http://catalog.local/ordinary-public-request")
    assert "http://catalog.local/ordinary-public-request" in caplog.text


def test_overlapping_preview_requests_do_not_uninstall_each_others_redaction(
    install_transport,
    caplog,
):
    caplog.set_level(logging.INFO, logger="httpx2")
    started = {ref: threading.Event() for ref in (7, 8)}
    release = {ref: threading.Event() for ref in (7, 8)}
    failures = []
    results = []

    def handler(request):
        if request.method == "POST":
            outer = parse_qs(request.content.decode())
            inner = parse_qs(outer["query"][0])
            ref = int(inner["ref"][0])
            return httpx.Response(
                200,
                json=f"http://catalog.local/thumb/{ref}?access_key=TEMPORARY-SECRET-{ref}",
            )
        if request.url.path == "/ordinary-public-request":
            return httpx.Response(200)
        ref = int(request.url.path.rsplit("/", 1)[1])
        started[ref].set()
        if not release[ref].wait(timeout=10):
            raise RuntimeError("Preview test synchronization timed out.")
        return httpx.Response(200, content=b"preview")

    def worker(ref):
        try:
            client = ResourceSpaceClient("http://catalog.local", "reader", "key")
            results.append(client.thumbnail(ref))
        except Exception as exc:
            failures.append(exc)

    install_transport(handler)
    threads = {ref: threading.Thread(target=worker, args=(ref,)) for ref in (7, 8)}
    for thread in threads.values():
        thread.start()
    try:
        assert all(event.wait(timeout=5) for event in started.values())
        release[7].set()
        threads[7].join(timeout=5)
        assert not threads[7].is_alive()
        assert threads[8].is_alive()
        # An unrelated request in this thread remains visible while the second
        # preview thread is active; one context must not silence another.
        with httpx.Client() as ordinary:
            ordinary.get("http://catalog.local/ordinary-public-request")
    finally:
        for event in release.values():
            event.set()
        for thread in threads.values():
            thread.join(timeout=10)
    assert failures == []
    assert results == [b"preview", b"preview"]
    assert "TEMPORARY-SECRET" not in caplog.text
    assert "http://catalog.local/ordinary-public-request" in caplog.text
