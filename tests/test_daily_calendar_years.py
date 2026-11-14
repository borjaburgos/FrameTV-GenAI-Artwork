"""Calendar commissioning and January rollover must not rewrite holiday dates."""

from datetime import datetime, timezone

from frameart.daily_art import DailyArtStore, DailyArtWorker


def calendar(year, day):
    return {
        "year": year,
        "timezone": "America/New_York",
        "entries": [
            {"date": day, "artwork_id": "one-shared-work"},
        ],
    }


def test_adjacent_years_coexist_and_roll_over_without_reimport(tmp_path):
    store = DailyArtStore(tmp_path)
    store.set_calendar(calendar(2026, "2026-12-31"))
    store.set_calendar(calendar(2027, "2027-01-01"))
    store.configure({"enabled": True, "target_id": "family-room"})
    before = store.snapshot(datetime(2026, 12, 31, 15, tzinfo=timezone.utc))
    assert before["calendar_year"] == 2026
    assert before["calendar_years"] == [2026, 2027]
    assert before["total_calendar_count"] == 2
    assert before["tomorrow"]["artwork_id"] == "one-shared-work"
    calls = []

    def display(artwork, kind, target, day, *, skip_tvs):
        calls.append(day)
        return {"results": [{"tv": "family-room", "status": "displayed"}]}

    settings = type("Settings", (), {"data_dir": tmp_path})()
    worker = DailyArtWorker(lambda: settings, display)
    worker.tick(datetime(2026, 12, 31, 15, tzinfo=timezone.utc))
    worker.tick(datetime(2027, 1, 1, 15, tzinfo=timezone.utc))
    assert calls == ["2026-12-31", "2027-01-01"]
    assert store.snapshot(datetime(2027, 1, 1, 15, tzinfo=timezone.utc))["calendar_year"] == 2027


def test_replacing_one_year_preserves_the_other_year(tmp_path):
    store = DailyArtStore(tmp_path)
    store.set_calendar(calendar(2026, "2026-12-31"))
    store.set_calendar(calendar(2027, "2027-01-01"))
    store.set_calendar(calendar(2027, "2027-02-01"))
    state = store.snapshot(datetime(2026, 12, 31, 15, tzinfo=timezone.utc))
    assert state["today"]["artwork_id"] == "one-shared-work"
    assert state["tomorrow"]["artwork_id"] is None
    assert state["total_calendar_count"] == 2
