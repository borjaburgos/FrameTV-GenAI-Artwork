"""Tests for persistent library metadata and display history."""

from frameart.library import LibraryStore


def test_tags_and_collections_round_trip(tmp_path):
    store = LibraryStore(tmp_path)
    assert store.set_tags("job-1", [" Travel ", "travel", "Blue"]) == ["travel", "blue"]

    collection = store.create_collection("Favorites")
    store.add_collection_items(str(collection["id"]), ["job-1"])
    metadata = store.metadata_for_jobs(["job-1"])

    assert metadata["job-1"]["tags"] == ["blue", "travel"]
    assert metadata["job-1"]["collections"] == ["Favorites"]
    assert store.list_collections()[0]["item_count"] == 1

    store.remove_collection_items(str(collection["id"]), ["job-1"])
    assert store.collection_job_ids(str(collection["id"])) == set()
    assert store.delete_collection(str(collection["id"])) is True


def test_display_history_is_newest_first(tmp_path):
    store = LibraryStore(tmp_path)
    store.record_display(
        job_id="job-1",
        content_id="content-1",
        tv_target="living-room",
        source="library-upload",
    )
    store.record_display(
        job_id=None,
        content_id="content-2",
        tv_target="bedroom",
        source="tv-art",
    )

    history = store.list_history()
    assert [item["content_id"] for item in history] == ["content-2", "content-1"]


def test_remove_job_cleans_library_metadata(tmp_path):
    store = LibraryStore(tmp_path)
    collection = store.create_collection("Later")
    store.set_tags("job-1", ["calm"])
    store.add_collection_items(str(collection["id"]), ["job-1"])

    store.remove_job("job-1")

    assert store.metadata_for_jobs(["job-1"])["job-1"] == {
        "tags": [],
        "collections": [],
    }
