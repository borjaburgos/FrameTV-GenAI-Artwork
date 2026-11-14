"""The pilot never ingests originals or silently expands past 20 artworks."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs

import httpx2 as httpx
import pytest
import yaml
from PIL import Image

from scripts import resourcespace_pilot as pilot


@pytest.fixture
def pilot_files(tmp_path):
    originals = tmp_path / "originals"
    renditions = tmp_path / "renditions"
    originals.mkdir()
    renditions.mkdir()
    buffer = BytesIO()
    Image.new("RGB", (3840, 2160), "#234567").save(buffer, "JPEG")
    jpeg = buffer.getvalue()
    items = []
    for number in range(20):
        original = originals / f"source-{number}.jpg"
        rendition = renditions / f"tv-{number}.jpg"
        original.write_bytes(b"original-only-" + str(number).encode())
        rendition.write_bytes(jpeg)
        items.append(
            {
                "metadata": {
                    "schema_version": 1,
                    "id": f"pilot-{number}",
                    "title": f"Artwork {number}",
                    "artist": {
                        "id": "artist-example",
                        "name": "Example Artist",
                        "bio": "Verified.",
                    },
                    "date": "1906",
                    "medium": "Oil on canvas",
                    "ai_generated": False,
                    "license": "Public Domain (CC0)",
                    "width": 3840,
                    "height": 2160,
                },
                "original_path": original.name,
                "rendition_path": rendition.name,
                "original_sha256": pilot.file_sha256(original),
                "rendition_sha256": pilot.file_sha256(rendition),
            }
        )
    document = {"schema_version": 1, "collection_name": "FrameArt pilot test", "items": items}
    manifest = tmp_path / "pilot.json"
    manifest.write_text(json.dumps(document))
    return manifest, originals, renditions, document


def load_files(files, mutate=None):
    path, originals, renditions, document = files
    if mutate:
        mutate(document)
        path.write_text(json.dumps(document))
    return pilot.load_manifest(path, originals, renditions)


class FakeAPI:
    def __init__(self):
        self.fields = [
            {"name": "title", "ref": 8, "type": 0},
            {"name": "caption", "ref": 18, "type": 1},
        ]
        self.resources = {}
        self.members = []
        self.collections = []
        self.uploads = []
        self.calls = []

    def call(self, function, *, upload=None, **params):
        self.calls.append(function)
        if function == "get_resource_type_fields":
            return self.fields
        if function == "create_resource_type_field":
            ref = 100 + len(self.fields)
            self.fields.append(
                {
                    "name": params["name"].replace("_", "").replace("-", ""),
                    "type": params["type"],
                    "ref": ref,
                }
            )
            return {"status": "success", "data": {"ref": ref}}
        if function == "get_user_collections":
            return self.collections
        if function == "create_collection":
            self.collections.append({"name": params["name"], "ref": 7})
            return 7
        if function == "do_search":
            if params["search"].startswith("!collection"):
                refs = self.members
            else:
                artwork_id = params["search"].split(":", 1)[1]
                refs = [
                    ref
                    for ref, data in self.resources.items()
                    if data["fields"].get("frameartid") == artwork_id
                ]
            return {"total": len(refs), "data": [{"ref": ref} for ref in refs]}
        if function == "create_resource":
            ref = 1000 + len(self.resources)
            names = {str(field["ref"]): field["name"] for field in self.fields}
            values = json.loads(params["metadata"])
            self.resources[ref] = {
                "fields": {names[key]: value for key, value in values.items()},
                "file_extension": "",
            }
            return ref
        if function == "get_resource_field_data":
            return [
                {"name": name, "value": value}
                for name, value in self.resources[params["resource"]]["fields"].items()
            ]
        if function == "add_resource_to_collection":
            if params["resource"] not in self.members:
                self.members.append(params["resource"])
            return True
        if function == "get_resource_data":
            return {"file_extension": self.resources[params["resource"]]["file_extension"]}
        if function == "upload_file":
            self.uploads.append(upload)
            self.resources[params["ref"]]["file_extension"] = "jpg"
            return params["ref"]
        if function == "update_field":
            field = next(row["name"] for row in self.fields if row["ref"] == params["field"])
            self.resources[params["resource"]]["fields"][field] = params["value"]
            return True
        raise AssertionError(f"Unexpected API function: {function}")


def test_manifest_validates_twenty_real_renditions_without_touching_originals(pilot_files):
    manifest = load_files(pilot_files)
    assert len(manifest.items) == 20
    assert manifest.items[0].metadata["schema_version"] == 1
    assert (
        manifest.items[0].metadata["original_sha256"]
        == pilot_files[3]["items"][0]["original_sha256"]
    )
    assert manifest.items[0].original.read_bytes() == b"original-only-0"


@pytest.mark.parametrize("size", [0, 19, 21])
def test_manifest_requires_exactly_twenty(pilot_files, size):
    def mutate(document):
        document["items"] = (document["items"] * 2)[:size]

    with pytest.raises(pilot.PilotError, match="exactly 20"):
        load_files(pilot_files, mutate)


@pytest.mark.parametrize("path", ["../outside.jpg", "/etc/passwd", "..\\outside.jpg"])
def test_manifest_rejects_path_escape(pilot_files, path):
    with pytest.raises(pilot.PilotError, match="relative file path|cannot escape"):
        load_files(pilot_files, lambda data: data["items"][0].update(original_path=path))


def test_manifest_rejects_symlink_escape(pilot_files, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    (pilot_files[1] / "escape.jpg").symlink_to(outside)
    with pytest.raises(pilot.PilotError, match="inside its root"):
        load_files(pilot_files, lambda data: data["items"][0].update(original_path="escape.jpg"))


def test_manifest_rejects_duplicate_ids_and_bad_checksums(pilot_files):
    with pytest.raises(pilot.PilotError, match="duplicate artwork IDs"):
        load_files(pilot_files, lambda data: data["items"][1]["metadata"].update(id="pilot-0"))
    pilot_files[3]["items"][1]["metadata"]["id"] = "pilot-1"
    with pytest.raises(pilot.PilotError, match="checksum mismatch"):
        load_files(pilot_files, lambda data: data["items"][0].update(original_sha256="0" * 64))


def test_manifest_rejects_non_full_frame_images(pilot_files):
    Image.new("RGB", (800, 600)).save(pilot_files[2] / "tv-0.jpg")
    with pytest.raises(pilot.PilotError, match="3840×2160 RGB JPEG"):
        load_files(pilot_files)


def test_manifest_requires_truthful_ai_and_license_metadata(pilot_files):
    with pytest.raises(pilot.PilotError, match="explicit ai_generated"):
        load_files(pilot_files, lambda data: data["items"][0]["metadata"].pop("ai_generated"))
    pilot_files[3]["items"][0]["metadata"]["ai_generated"] = False
    with pytest.raises(pilot.PilotError, match="license is required"):
        load_files(pilot_files, lambda data: data["items"][0]["metadata"].update(license=""))


def test_structured_ai_provenance_is_portable_json_text(pilot_files):
    manifest = load_files(
        pilot_files,
        lambda data: data["items"][0]["metadata"].update(
            ai_generated=True, ai_provenance={"tool": "imagegen", "prompt": "Original geometry"}
        ),
    )
    assert json.loads(manifest.items[0].metadata["ai_provenance"])["tool"] == "imagegen"


@pytest.mark.parametrize(
    "field,value",
    [("title", "x" * 501), ("source_url", "javascript:alert(1)"), ("medium", "x" * 1001)],
)
def test_manifest_rejects_metadata_the_runtime_reader_cannot_accept(pilot_files, field, value):
    with pytest.raises(pilot.PilotError, match="FrameArt's artwork schema"):
        load_files(pilot_files, lambda data: data["items"][0]["metadata"].update({field: value}))


def test_manifest_rejects_oversized_artist_biography(pilot_files):
    with pytest.raises(pilot.PilotError, match="FrameArt's artwork schema"):
        load_files(
            pilot_files,
            lambda data: data["items"][0]["metadata"]["artist"].update(bio="x" * 5001),
        )


def test_dry_run_never_initializes_an_api_client(pilot_files, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("Dry-run must never open a network client")

    monkeypatch.setattr(pilot, "AdminAPI", unexpected)
    assert (
        pilot.main(
            [
                "--manifest",
                str(pilot_files[0]),
                "--originals-root",
                str(pilot_files[1]),
                "--renditions-root",
                str(pilot_files[2]),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["item_count"] == 20
    assert report["network_requests"] == 0
    assert report["dry_run"] is True


def test_import_only_uploads_renditions_and_is_idempotent(pilot_files):
    manifest = load_files(pilot_files)
    api = FakeAPI()
    first = pilot.import_pilot(manifest, api)
    second = pilot.import_pilot(manifest, api)
    assert first == second
    assert len(api.resources) == len(api.members) == len(api.uploads) == 20
    assert len(api.fields) == 2 + len(pilot.METADATA_FIELDS)
    assert all(path.parent == pilot_files[2] for path in api.uploads)
    assert all(item.original.read_bytes().startswith(b"original-only-") for item in manifest.items)
    assert "delete_resource" not in api.calls
    assert "replace_resource_file" not in api.calls
    stored = json.loads(api.resources[1000]["fields"]["frameartmetadata"])
    assert stored["artist"]["name"] == "Example Artist"
    assert stored["original_path"] == "source-0.jpg"


def test_generated_shortnames_are_resolved_without_creating_duplicate_fields():
    api = FakeAPI()
    first_refs, first_id_name = pilot.ensure_fields(api)
    second_refs, second_id_name = pilot.ensure_fields(api)
    assert first_refs == second_refs
    assert first_id_name == second_id_name == "frameartid"
    assert len(api.fields) == 2 + len(pilot.METADATA_FIELDS)
    assert {field["name"] for field in api.fields} >= {"frameartid", "frameartmetadata"}


def test_canonical_and_generated_duplicate_field_definitions_are_rejected():
    api = FakeAPI()
    pilot.ensure_fields(api)
    api.fields.append({"name": "frameart_metadata", "ref": 999, "type": 5})
    with pytest.raises(pilot.PilotError, match="ambiguous FrameArt metadata fields"):
        pilot.ensure_fields(api)


def test_import_refuses_foreign_or_changed_resources(pilot_files):
    manifest = load_files(pilot_files)
    api = FakeAPI()
    pilot.import_pilot(manifest, api)
    api.resources[1000]["fields"]["frameartid"] = "other-owner"
    with pytest.raises(pilot.PilotError, match="foreign or duplicate"):
        pilot.import_pilot(manifest, api)
    api.resources[1000]["fields"]["frameartid"] = "pilot-0"
    stored = json.loads(api.resources[1000]["fields"]["frameartmetadata"])
    stored["rendition_sha256"] = "0" * 64
    api.resources[1000]["fields"]["frameartmetadata"] = json.dumps(stored)
    with pytest.raises(pilot.PilotError, match="refusing to overwrite"):
        pilot.import_pilot(manifest, api)
    assert len(api.uploads) == 20


def test_import_rechecks_files_before_any_upload(pilot_files):
    manifest = load_files(pilot_files)
    manifest.items[0].original.write_bytes(b"changed after initial validation")
    api = FakeAPI()
    with pytest.raises(pilot.PilotError, match="checksum mismatch"):
        pilot.import_pilot(manifest, api)
    assert api.uploads == []


def test_import_recovers_interrupted_upload_without_duplicate_resources(pilot_files):
    manifest = load_files(pilot_files)
    api = FakeAPI()
    pilot.import_pilot(manifest, api)
    api.resources[1000]["file_extension"] = ""
    api.members.remove(1000)
    pilot.import_pilot(manifest, api)
    assert len(api.resources) == len(api.members) == 20
    assert len(api.uploads) == 21


def test_admin_api_uses_signed_post_wrapper_and_does_not_send_the_key():
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json=[])

    api = pilot.AdminAPI("http://127.0.0.1:8187", "bootstrap", "test-secret")
    api.client.close()
    api.client = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        assert api.call("do_search", search="art & design", fetchrows="0,20") == []
    finally:
        api.close()
    request = captured[0]
    form = parse_qs(request.content.decode())
    unsigned = form["query"][0].rsplit("&sign=", 1)[0]
    assert form["sign"][0] == hashlib.sha256(("test-secret" + unsigned).encode()).hexdigest()
    assert b"test-secret" not in request.content
    assert request.method == "POST"
    assert not request.url.query


def test_admin_api_uploads_jpeg_as_multipart_not_a_server_side_path(pilot_files):
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json=1000)

    api = pilot.AdminAPI("http://127.0.0.1:8187", "bootstrap", "test-secret")
    api.client.close()
    api.client = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        api.call("upload_file", ref=1000, upload=pilot_files[2] / "tv-0.jpg", no_exif=True)
    finally:
        api.close()
    assert "multipart/form-data" in captured[0].headers["content-type"]
    assert b'name="userfile"' in captured[0].content
    assert b'filename="tv-0.jpg"' in captured[0].content
    assert b"original-only-" not in captured[0].content
    assert b"file_path=" not in captured[0].content


@pytest.mark.parametrize("url", ["file:///tmp/rs", "http://user:pass@host", "https://host?key=bad"])
def test_admin_api_rejects_unsafe_urls(url):
    with pytest.raises(pilot.PilotError, match="HTTP\\(S\\)"):
        pilot.AdminAPI(url, "bootstrap", "test-secret")


def test_compose_keeps_nas_and_renditions_read_only_and_database_private():
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "deploy/resourcespace/compose.yaml").read_text())
    services = compose["services"]
    mounts = services["resourcespace"]["volumes"]
    for target in ("/artwork-originals", "/artwork-renditions"):
        mount = next(entry for entry in mounts if entry["target"] == target)
        assert mount["read_only"] is True
        assert mount["bind"]["create_host_path"] is False
        assert ":?" in mount["source"]
    assert services["resourcespace"]["ports"] == [
        "${RESOURCESPACE_BIND_ADDRESS:-127.0.0.1}:${RESOURCESPACE_PORT:-8187}:80"
    ]
    assert services["resourcespace"]["environment"]["RESOURCESPACE_BASE_URL"] == (
        "${RESOURCESPACE_BASE_URL:-http://127.0.0.1:8187}"
    )
    assert "ports" not in services["mariadb"]
    assert compose["networks"]["backend"]["internal"] is True
    assert services["mariadb"]["environment"]["MARIADB_PASSWORD_FILE"].startswith("/run/secrets/")
    entrypoint = (root / "deploy/resourcespace/pilot-entrypoint.sh").read_text().lower()
    assert "php pages/tools/staticsync.php" not in entrypoint
