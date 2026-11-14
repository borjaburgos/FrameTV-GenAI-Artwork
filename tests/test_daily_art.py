"""Synthetic Daily Art state-machine tests; no network or household fixtures."""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from frameart.daily_art import DailyArtStore, DailyArtWorker


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def calendar(*days: str) -> dict:
    return {
        "year": int(days[0][:4]),
        "timezone": "America/New_York",
        "entries": [
            {
                "date": day,
                "artwork_id": "art-" + day,
                "month": int(day[5:7]),
                "season": "winter",
                "occasion": "Synthetic test",
            }
            for day in days
        ],
    }


class TestDailyArt(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="frameart-daily-test-")
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.store = DailyArtStore(self.data_dir)
        self.settings = SimpleNamespace(data_dir=self.data_dir)
        self.calls = []

    def prepare(self, *days, target_type="tv", target_id="family-room", **settings):
        self.store.set_calendar(calendar(*(days or ("2027-01-01", "2027-01-02"))))
        self.store.configure(
            {
                "enabled": True,
                "target_type": target_type,
                "target_id": target_id,
                "change_time": "09:00",
                **settings,
            }
        )

    def deliver(self, artwork_id, target_type, target_id, day_key, *, skip_tvs):
        self.calls.append((artwork_id, target_type, target_id, day_key, skip_tvs))
        return {"results": [{"tv": target_id, "status": "displayed"}]}

    def worker(self, deliver=None, *, reconcile=None, retry_seconds=60):
        return DailyArtWorker(
            lambda: self.settings,
            deliver or self.deliver,
            reconcile=reconcile,
            retry_seconds=retry_seconds,
        )

    def test_default_disabled_configuration_does_not_deliver(self):
        self.assertFalse(self.store.config()["enabled"])
        self.assertIsNone(self.store.config()["target_id"])
        self.assertEqual(self.store.config()["change_time"], "00:00")
        snapshot = self.worker().tick(instant("2027-01-01T15:00:00+00:00"))
        self.assertEqual(snapshot["calendar_count"], 0)
        self.assertEqual(snapshot["today"]["status"], "not_covered")
        self.assertEqual(self.calls, [])

    def test_preferences_directory_and_database_are_private(self):
        self.assertEqual(self.data_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.data_dir.chmod(0o755)
        self.store.path.chmod(0o644)
        DailyArtStore(self.data_dir)
        self.assertEqual(self.data_dir.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        newly_created = self.data_dir / "new-private-directory"
        DailyArtStore(newly_created)
        self.assertEqual(newly_created.stat().st_mode & 0o777, 0o700)

    def test_local_time_gate_and_today_tomorrow(self):
        self.prepare()
        worker = self.worker()
        before = worker.tick(instant("2027-01-01T13:59:00+00:00"))
        self.assertEqual(before["today"]["status"], "scheduled")
        self.assertEqual(before["tomorrow"]["artwork_id"], "art-2027-01-02")
        after = worker.tick(instant("2027-01-01T14:00:00+00:00"))
        self.assertEqual(after["today"]["status"], "displayed")
        self.assertEqual(self.calls[0][0], "art-2027-01-01")

    def test_does_not_remap_2027_calendar_into_2026(self):
        self.prepare()
        result = self.worker().tick(instant("2026-01-01T15:00:00+00:00"))
        self.assertFalse(result["calendar_covered"])
        self.assertEqual(result["calendar_year"], 2027)
        self.assertEqual(self.calls, [])

    def test_missed_days_do_not_cycle_or_replay(self):
        self.prepare("2027-01-01", "2027-01-02", "2027-01-03")
        self.worker().tick(instant("2027-01-03T15:00:00+00:00"))
        self.assertEqual([call[0] for call in self.calls], ["art-2027-01-03"])

    def test_restart_does_not_repeat_success(self):
        self.prepare()
        now = instant("2027-01-01T15:00:00+00:00")
        self.worker().tick(now)
        self.worker().tick(now + timedelta(hours=2))
        self.assertEqual(len(self.calls), 1)

    def test_backwards_clock_does_not_dispatch_yesterday(self):
        self.prepare("2027-01-01", "2027-01-02")
        self.worker().tick(instant("2027-01-02T15:00:00+00:00"))
        self.worker().tick(instant("2027-01-01T15:00:00+00:00"))
        self.assertEqual(len(self.calls), 1)

    def test_dst_spring_nonexistent_time_runs_once_after_jump(self):
        self.prepare("2027-03-14", change_time="02:30")
        worker = self.worker()
        worker.tick(instant("2027-03-14T06:59:00+00:00"))
        self.assertEqual(self.calls, [])
        worker.tick(instant("2027-03-14T07:00:00+00:00"))
        worker.tick(instant("2027-03-14T07:30:00+00:00"))
        self.assertEqual(len(self.calls), 1)

    def test_dst_fall_repeated_hour_never_duplicates(self):
        self.prepare("2027-11-07", change_time="01:30")
        worker = self.worker()
        worker.tick(instant("2027-11-07T05:30:00+00:00"))
        worker.tick(instant("2027-11-07T06:30:00+00:00"))
        self.assertEqual(len(self.calls), 1)

    def test_busy_tv_defers_same_days_art_with_cooldown(self):
        self.prepare()
        attempts = []

        def delivery(*args, skip_tvs):
            attempts.append(args[0])
            return {
                "results": [
                    {"tv": "family-room", "status": "deferred", "detail": "TV is being watched"}
                ]
            }

        worker = self.worker(delivery)
        now = instant("2027-01-01T15:00:00+00:00")
        result = worker.tick(now)
        self.assertEqual(result["today"]["status"], "deferred")
        worker.tick(now + timedelta(seconds=59))
        self.assertEqual(len(attempts), 1)
        self.worker(delivery).tick(now + timedelta(seconds=60))
        self.assertEqual(attempts, ["art-2027-01-01", "art-2027-01-01"])

    def test_partial_group_only_retries_unfinished_tvs(self):
        self.prepare(target_type="group", target_id="both-frames")
        skip_values = []

        def delivery(*args, skip_tvs):
            skip_values.append(skip_tvs)
            results = [{"tv": "bedroom", "status": "displayed" if skip_tvs else "deferred"}]
            if not skip_tvs:
                results.append({"tv": "family-room", "status": "displayed"})
            return {"results": results}

        now = instant("2027-01-01T15:00:00+00:00")
        first = self.worker(delivery).tick(now)
        self.assertEqual(first["today"]["status"], "partial")
        second = self.worker(delivery).tick(now + timedelta(seconds=60))
        self.assertEqual(second["today"]["status"], "displayed")
        self.assertEqual(skip_values, [set(), {"family-room"}])

    def test_uncertain_tv_is_not_retried_but_other_tv_can_progress(self):
        self.prepare(target_type="group", target_id="both-frames")
        calls = []

        def delivery(*args, skip_tvs):
            calls.append(skip_tvs)
            if not skip_tvs:
                return {
                    "results": [
                        {"tv": "family-room", "status": "uncertain"},
                        {"tv": "bedroom", "status": "deferred"},
                    ]
                }
            return {"results": [{"tv": "bedroom", "status": "displayed"}]}

        now = instant("2027-01-01T15:00:00+00:00")
        self.worker(delivery).tick(now)
        result = self.worker(delivery).tick(now + timedelta(seconds=60))
        self.worker(delivery).tick(now + timedelta(hours=2))
        self.assertEqual(calls, [set(), {"family-room"}])
        self.assertEqual(result["today"]["status"], "uncertain")

    def test_explicit_inventory_reconciliation_unblocks_uncertain_tv(self):
        self.prepare()
        now = instant("2027-01-01T15:00:00+00:00")
        self.worker(
            lambda *args, **kwargs: {"results": [{"tv": "family-room", "status": "uncertain"}]}
        ).tick(now)
        self.store.resolve_uncertain("2027-01-01", "family-room", displayed=False)
        result = self.worker().tick(now + timedelta(seconds=1))
        self.assertEqual(result["today"]["status"], "displayed")

    def test_callback_exception_is_non_replayable_after_restart(self):
        self.prepare()
        attempts = []

        def delivery(*args, **kwargs):
            attempts.append(1)
            raise RuntimeError("sensitive upstream detail must never be persisted")

        now = instant("2027-01-01T15:00:00+00:00")
        first = self.worker(delivery).tick(now)
        self.worker(delivery).tick(now + timedelta(hours=1))
        self.assertEqual(first["today"]["status"], "uncertain_dispatch")
        self.assertEqual(attempts, [1])
        self.assertNotIn("sensitive", str(first))

    def test_unfinished_committed_claim_is_never_replayed(self):
        self.prepare()
        now = instant("2027-01-01T15:00:00+00:00")
        self.assertIsNotNone(self.store._claim(now, 60))
        self.worker().tick(now + timedelta(hours=1))
        self.assertEqual(self.calls, [])

    def test_malformed_result_is_not_replayed(self):
        self.prepare()
        now = instant("2027-01-01T15:00:00+00:00")
        result = self.worker(lambda *args, **kwargs: {"results": []}).tick(now)
        self.worker().tick(now + timedelta(hours=1))
        self.assertEqual(result["today"]["status"], "uncertain_dispatch")
        self.assertEqual(self.calls, [])

    def test_wrong_tv_result_cannot_mark_target_as_complete(self):
        self.prepare()
        now = instant("2027-01-01T15:00:00+00:00")
        result = self.worker(
            lambda *args, **kwargs: {"results": [{"tv": "bedroom", "status": "displayed"}]}
        ).tick(now)
        self.assertEqual(result["today"]["status"], "uncertain_dispatch")
        self.assertEqual(result["results"], [])

    def test_two_workers_cannot_concurrently_dispatch_same_day(self):
        self.prepare()
        entered, release = threading.Event(), threading.Event()
        attempts = []

        def delivery(*args, **kwargs):
            attempts.append(1)
            entered.set()
            release.wait(timeout=3)
            return {"results": [{"tv": "family-room", "status": "displayed"}]}

        now = instant("2027-01-01T15:00:00+00:00")
        thread = threading.Thread(target=lambda: self.worker(delivery).tick(now))
        thread.start()
        self.assertTrue(entered.wait(timeout=3))
        self.worker(delivery).tick(now)
        release.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(attempts, [1])

    def test_manual_choice_holds_tv_through_its_local_day(self):
        self.prepare()
        now = instant("2027-01-01T15:00:00+00:00")
        self.store.note_manual_choice("family-room", now)
        result = self.worker().tick(now)
        self.assertEqual(result["today"]["status"], "held")
        self.assertEqual(result["holds"], [{"tv": "family-room", "date": "2027-01-01"}])
        self.assertEqual(self.calls, [])
        self.worker().tick(instant("2027-01-02T15:00:00+00:00"))
        self.assertEqual(len(self.calls), 1)

    def test_regular_config_save_preserves_manual_hold_explicit_clear_resumes(self):
        self.prepare()
        now = instant("2027-01-01T15:00:00+00:00")
        self.store.note_manual_choice("family-room", now)
        self.store.configure({"change_time": "08:00"})
        self.worker().tick(now)
        self.assertEqual(self.calls, [])
        self.store.configure({"clear_holds": True})
        self.worker().tick(now)
        self.assertEqual(len(self.calls), 1)

    def test_group_manual_hold_clear_reopens_only_unfinished_tv(self):
        self.prepare(target_type="group", target_id="both-frames")
        now = instant("2027-01-01T15:00:00+00:00")
        self.store.note_manual_choice("family-room", now)
        skips = []

        def delivery(*args, skip_tvs):
            skips.append(skip_tvs)
            return {
                "results": [
                    {"tv": tv, "status": "displayed"}
                    for tv in ("family-room", "bedroom")
                    if tv not in skip_tvs
                ]
            }

        self.worker(delivery).tick(now)
        self.store.configure({"clear_holds": True})
        self.worker(delivery).tick(now + timedelta(seconds=1))
        self.assertEqual(skips, [{"family-room"}, {"bedroom"}])

    def test_global_hold_requires_aware_timestamp_and_defers_without_claim(self):
        self.prepare(hold_until="2027-01-01T16:00:00Z")
        worker = self.worker()
        before = worker.tick(instant("2027-01-01T15:00:00+00:00"))
        self.assertEqual(before["today"]["status"], "held")
        self.assertEqual(self.calls, [])
        worker.tick(instant("2027-01-01T16:00:00+00:00"))
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ValueError):
            self.store.configure({"hold_until": "2027-01-01T12:00:00"})

    def test_calendar_validation_is_atomic(self):
        self.prepare()
        duplicate = calendar("2027-02-01", "2027-02-02")
        duplicate["entries"][1]["artwork_id"] = duplicate["entries"][0]["artwork_id"]
        with self.assertRaises(ValueError):
            self.store.set_calendar(duplicate)
        with self.assertRaises(ValueError):
            self.store.set_calendar(
                {"year": 2027, "entries": [{"date": "2027-02-29", "artwork_id": "a"}]}
            )
        snapshot = self.store.snapshot(instant("2027-01-01T15:00:00+00:00"))
        self.assertEqual(snapshot["calendar_count"], 2)

    def test_leap_day_calendar_is_valid_only_in_a_leap_year(self):
        self.store.set_calendar(calendar("2028-02-29"))
        self.assertEqual(
            self.store.snapshot(instant("2028-02-29T15:00:00+00:00"))["today"]["artwork_id"],
            "art-2028-02-29",
        )

    def test_cannot_replace_artwork_after_delivery_attempt(self):
        self.prepare()
        self.worker().tick(instant("2027-01-01T15:00:00+00:00"))
        replacement = calendar("2027-01-01")
        replacement["entries"][0]["artwork_id"] = "changed"
        with self.assertRaises(ValueError):
            self.store.set_calendar(replacement)

    def test_invalid_settings_rejected_without_overwriting_previous(self):
        for invalid in (
            {"enabled": "yes"},
            {"timezone": "Not/AZone"},
            {"change_time": "24:00"},
            {"target_type": "all"},
            {"enabled": True},
            {"target_id": "../unsafe"},
            {"credentials": "forbidden"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.configure(invalid)
        self.assertFalse(self.store.config()["enabled"])

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.snapshot(datetime(2027, 1, 1))

    def test_definite_errors_retry_each_remaining_hour_of_a_long_dst_day(self):
        self.prepare("2027-11-07", change_time="00:00")
        attempts = []

        def delivery(*args, **kwargs):
            attempts.append(1)
            return {"results": [{"tv": "family-room", "status": "error"}]}

        now = instant("2027-11-07T04:00:00+00:00")
        worker = DailyArtWorker(lambda: self.settings, delivery)
        worker.tick(now)
        worker.tick(now + timedelta(seconds=3599))
        self.assertEqual(len(attempts), 1)
        for hours in range(1, 25):
            worker.tick(now + timedelta(hours=hours))
        self.assertEqual(len(attempts), 25)
        worker.tick(now + timedelta(hours=25))
        self.assertEqual(len(attempts), 25)

    def test_default_schedule_starts_at_local_midnight(self):
        self.store.set_calendar(calendar("2027-01-01"))
        self.store.configure({"enabled": True, "target_id": "family-room"})
        worker = DailyArtWorker(lambda: self.settings, self.deliver)
        self.assertEqual(worker.retry_seconds, 3600)
        worker.tick(instant("2027-01-01T04:59:59+00:00"))
        self.assertEqual(self.calls, [])
        worker.tick(instant("2027-01-01T05:00:00+00:00"))
        self.assertEqual(len(self.calls), 1)

    def test_default_busy_retry_is_hourly_and_keeps_completed_group_tv_skipped(self):
        self.prepare(target_type="group", target_id="both-frames", change_time="00:00")
        skips = []

        def delivery(*args, skip_tvs):
            skips.append(skip_tvs)
            results = [{"tv": "bedroom", "status": "displayed" if skip_tvs else "deferred"}]
            if not skip_tvs:
                results.append({"tv": "family-room", "status": "displayed"})
            return {"results": results}

        worker = DailyArtWorker(lambda: self.settings, delivery)
        now = instant("2027-01-01T05:00:00+00:00")
        first = worker.tick(now)
        self.assertEqual(first["next_retry_at"], "2027-01-01T06:00:00+00:00")
        worker.tick(now + timedelta(seconds=3599))
        self.assertEqual(len(skips), 1)
        worker.tick(now + timedelta(hours=1))
        self.assertEqual(skips, [set(), {"family-room"}])

    def test_uncertain_reconciliation_runs_hourly_and_can_confirm_display_without_replay(self):
        self.prepare(change_time="00:00")
        mutations, reads = [], []

        def delivery(*args, **kwargs):
            mutations.append(1)
            return {"results": [{"tv": "family-room", "status": "uncertain"}]}

        def reconcile(*args):
            reads.append(args)
            return "displayed"

        worker = DailyArtWorker(lambda: self.settings, delivery, reconcile=reconcile)
        now = instant("2027-01-01T05:00:00+00:00")
        first = worker.tick(now)
        self.assertTrue(first["pending"])
        self.assertEqual(first["next_retry_at"], "2027-01-01T06:00:00+00:00")
        worker.tick(now + timedelta(seconds=3599))
        self.assertEqual(reads, [])
        result = worker.tick(now + timedelta(hours=1))
        self.assertEqual(result["today"]["status"], "displayed")
        self.assertEqual(mutations, [1])
        self.assertEqual(
            reads, [("art-2027-01-01", "tv", "family-room", "2027-01-01", "family-room")]
        )

    def test_only_positive_retryable_reconciliation_allows_another_guarded_delivery(self):
        self.prepare(change_time="00:00")
        mutations, reads = [], []

        def delivery(*args, skip_tvs):
            mutations.append(skip_tvs)
            return {
                "results": [
                    {
                        "tv": "family-room",
                        "status": "uncertain" if len(mutations) == 1 else "displayed",
                    }
                ]
            }

        def reconcile(*args):
            reads.append(1)
            return "retryable"

        worker = DailyArtWorker(lambda: self.settings, delivery, reconcile=reconcile)
        now = instant("2027-01-01T05:00:00+00:00")
        worker.tick(now)
        result = worker.tick(now + timedelta(hours=1))
        worker.tick(now + timedelta(hours=2))
        self.assertEqual(result["today"]["status"], "displayed")
        self.assertEqual(mutations, [set(), set()])
        self.assertEqual(reads, [1])

    def test_unknown_uncertain_outcome_gets_hourly_read_checks_never_blind_mutation(self):
        self.prepare(change_time="00:00")
        mutations, reads = [], []

        def delivery(*args, **kwargs):
            mutations.append(1)
            return {"results": [{"tv": "family-room", "status": "uncertain"}]}

        def reconcile(*args):
            reads.append(1)
            return "uncertain"

        worker = DailyArtWorker(lambda: self.settings, delivery, reconcile=reconcile)
        now = instant("2027-01-01T05:00:00+00:00")
        worker.tick(now)
        for hours in range(1, 5):
            result = worker.tick(now + timedelta(hours=hours))
            worker.tick(now + timedelta(hours=hours, seconds=1))
        self.assertEqual(result["today"]["status"], "uncertain")
        self.assertEqual(reads, [1, 1, 1, 1])
        self.assertEqual(mutations, [1])

    def test_reconciler_exception_or_malformed_answer_stays_readonly_and_retries_hourly(self):
        self.prepare(change_time="00:00")
        mutations, reads = [], []

        def delivery(*args, **kwargs):
            mutations.append(1)
            return {"results": [{"tv": "family-room", "status": "uncertain"}]}

        def reconcile(*args):
            reads.append(1)
            if len(reads) == 1:
                raise RuntimeError("private upstream token must not be persisted")
            return {"unexpected": "result"}

        worker = DailyArtWorker(lambda: self.settings, delivery, reconcile=reconcile)
        now = instant("2027-01-01T05:00:00+00:00")
        worker.tick(now)
        worker.tick(now + timedelta(hours=1))
        result = worker.tick(now + timedelta(hours=2))
        self.assertEqual(result["today"]["status"], "uncertain")
        self.assertEqual(len(reads), 2)
        self.assertEqual(mutations, [1])
        self.assertNotIn("private upstream", str(result))

    def test_no_reconciler_keeps_uncertain_outcome_fail_closed(self):
        self.prepare(change_time="00:00")
        mutations = []

        def delivery(*args, **kwargs):
            mutations.append(1)
            return {"results": [{"tv": "family-room", "status": "uncertain"}]}

        worker = DailyArtWorker(lambda: self.settings, delivery)
        now = instant("2027-01-01T05:00:00+00:00")
        worker.tick(now)
        result = worker.tick(now + timedelta(hours=2))
        self.assertEqual(result["today"]["status"], "uncertain")
        self.assertIsNone(result["next_retry_at"])
        self.assertEqual(mutations, [1])

    def test_enabling_readonly_reconciler_after_restart_can_revisit_old_uncertain(self):
        self.prepare(change_time="00:00")
        mutations, reads = [], []

        def delivery(*args, **kwargs):
            mutations.append(1)
            return {"results": [{"tv": "family-room", "status": "uncertain"}]}

        def reconcile(*args):
            reads.append(1)
            return "displayed"

        now = instant("2027-01-01T05:00:00+00:00")
        DailyArtWorker(lambda: self.settings, delivery).tick(now)
        worker = DailyArtWorker(lambda: self.settings, delivery, reconcile=reconcile)
        worker.tick(now + timedelta(seconds=3599))
        self.assertEqual(reads, [])
        result = worker.tick(now + timedelta(hours=1))
        self.assertEqual(result["today"]["status"], "displayed")
        self.assertEqual(mutations, [1])

    def test_whole_uncertain_dispatch_remains_held_without_target_evidence(self):
        self.prepare(change_time="00:00")
        mutations, reads = [], []

        def delivery(*args, **kwargs):
            mutations.append(1)
            raise RuntimeError("outcome unknown")

        def reconcile(*args):
            reads.append(1)
            return "retryable"

        worker = DailyArtWorker(lambda: self.settings, delivery, reconcile=reconcile)
        now = instant("2027-01-01T05:00:00+00:00")
        worker.tick(now)
        result = worker.tick(now + timedelta(hours=1))
        self.assertEqual(result["today"]["status"], "uncertain_dispatch")
        self.assertEqual(mutations, [1])
        self.assertEqual(reads, [])


if __name__ == "__main__":
    unittest.main()
