import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src import database as db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_fresh_schema_contains_travel_class(self):
        conn = sqlite3.connect(db.DB_PATH)
        try:
            price_columns = {row[1] for row in conn.execute("PRAGMA table_info(price_history)")}
            monitor_columns = {row[1] for row in conn.execute("PRAGMA table_info(monitors)")}
        finally:
            conn.close()
        self.assertIn("travel_class", price_columns)
        self.assertIn("travel_class", monitor_columns)
        self.assertIn("schedule_effective_at", monitor_columns)
        self.assertEqual(db.DATABASE_SCHEMA_VERSION, 8)

    def test_monitor_and_price_support_selected_class(self):
        route_id = db.upsert_route("TPE", "KIX")
        monitor = db.add_monitor(route_id, "2026-09-01", 8000, travel_class=3)
        db.add_price_record(route_id, "2026-09-01", 9200, travel_class=3)
        row = db.list_monitors()[0]
        self.assertTrue(monitor["created"])
        self.assertEqual(row["travel_class"], 3)

    def test_delete_monitor_removes_dependent_notifications(self):
        route_id = db.upsert_route("TPE", "BKK")
        monitor_id = db.add_monitor(route_id, "2026-10-01", 9000)["id"]
        db.log_notification(monitor_id, "test", "local", True)
        db.delete_monitor(monitor_id)
        self.assertEqual(db.list_notifications(), [])

    def test_notification_claim_is_atomic(self):
        route_id = db.upsert_route("TPE", "NRT")
        monitor_id = db.add_monitor(route_id, "2026-11-01", 10000)["id"]
        self.assertTrue(db.claim_notification(monitor_id, "same-window"))
        self.assertFalse(db.claim_notification(monitor_id, "same-window"))

    def test_exact_history_duplicates_are_removed(self):
        route_id = db.upsert_route("TPE", "ICN")
        monitor_id = db.add_monitor(route_id, "2026-12-01", 11000)["id"]
        db.add_price_record(route_id, "2026-12-01", 12000)
        db.log_notification(monitor_id, "duplicate", "telegram", True)

        with db.get_db() as conn:
            for table in ("notifications", "price_history"):
                columns = [
                    row["name"]
                    for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                    if row["name"] != "id"
                ]
                names = ", ".join(f'"{column}"' for column in columns)
                conn.execute(
                    f'INSERT INTO "{table}" ({names}) SELECT {names} FROM "{table}" LIMIT 1'
                )

        deleted = db.deduplicate_exact_history()
        self.assertEqual(deleted, {"notifications": 1, "price_history": 1})
        with db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)

    def test_api_usage_conservatively_counts_every_attempt_against_quota(self):
        stamp = datetime(2026, 8, 11, 9, 0)
        db.record_api_usage("serpapi", "flight_search", True, created_at=stamp)
        db.record_api_usage("serpapi", "flight_search", False, created_at=stamp)
        summary = db.get_api_usage_summary("serpapi", 250, now=stamp)
        self.assertEqual(summary["used"], 2)
        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["remaining"], 248)

    def test_run_ledger_is_idempotent_per_scheduled_slot(self):
        route_id = db.upsert_route("TPE", "KIX")
        monitor_id = db.add_monitor(route_id, "2026-09-01", 10000)["id"]
        first = db.create_check_run(monitor_id, scheduled_slot="2026-08-12T12:00+08:00")
        second = db.create_check_run(monitor_id, scheduled_slot="2026-08-12T12:00+08:00")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        db.finish_check_run(
            first["id"],
            status="succeeded",
            api_calls=2,
            result={"price": 9000},
            lease_token=first["lease_token"],
        )
        saved = db.get_check_run(first["id"])
        self.assertEqual(saved["status"], "succeeded")
        self.assertEqual(saved["api_calls"], 2)

    def test_stale_running_check_lease_is_reclaimed_for_same_slot(self):
        route_id = db.upsert_route("TPE", "KIX")
        monitor_id = db.add_monitor(route_id, "2026-09-01", 10000)["id"]
        slot = "2026-08-12T12:00+08:00"
        stale_started = datetime.now().replace(microsecond=0) - timedelta(minutes=31)
        stale = db.create_check_run(
            monitor_id,
            scheduled_slot=slot,
            started_at=stale_started,
        )
        self.assertFalse(
            db.slot_has_run(
                monitor_id,
                slot,
                now=stale_started + timedelta(minutes=31),
            )
        )
        reclaimed = db.create_check_run(
            monitor_id,
            scheduled_slot=slot,
            started_at=stale_started + timedelta(minutes=31),
        )
        self.assertTrue(reclaimed["created"])
        self.assertTrue(reclaimed["reclaimed"])
        self.assertEqual(reclaimed["id"], stale["id"])
        self.assertTrue(
            db.slot_has_run(
                monitor_id,
                slot,
                now=stale_started + timedelta(minutes=32),
            )
        )

    def test_outbox_failed_delivery_gets_backoff_and_manual_retry(self):
        now = datetime.now().replace(microsecond=0)
        queued = db.enqueue_notification(
            channel="telegram",
            message="test",
            dedupe_key="outbox-one",
            max_attempts=2,
            next_attempt_at=now,
        )
        claimed = db.claim_due_notifications(now=now)
        self.assertEqual([item["id"] for item in claimed], [queued["id"]])
        failed = db.complete_outbox_delivery(
            queued["id"],
            success=False,
            error="timeout",
            now=now,
            lease_token=claimed[0]["lease_token"],
        )
        self.assertEqual(failed["status"], "retry")
        claimed_again = db.claim_due_notifications(now=now + timedelta(minutes=6))
        self.assertEqual(len(claimed_again), 1)
        final = db.complete_outbox_delivery(
            queued["id"],
            success=False,
            error="timeout",
            now=now + timedelta(minutes=6),
            lease_token=claimed_again[0]["lease_token"],
        )
        self.assertEqual(final["status"], "failed")
        db.retry_outbox_delivery(queued["id"])
        reset = db.list_notification_outbox()[0]
        self.assertEqual(reset["attempt_count"], 0)
        self.assertEqual(reset["status"], "retry")
        self.assertEqual(len(db.claim_due_notifications()), 1)

    def test_telegram_outbox_does_not_retry_abandoned_ambiguous_send(self):
        now = datetime.now().replace(microsecond=0)
        db.enqueue_notification(
            channel="telegram",
            message="recover me",
            dedupe_key="outbox-abandoned",
            max_attempts=3,
            next_attempt_at=now,
        )
        first = db.claim_due_notifications(now=now)
        self.assertEqual(first[0]["attempt_count"], 1)

        recovered = db.claim_due_notifications(
            now=now + timedelta(minutes=11), lease_timeout_minutes=10
        )
        self.assertEqual(recovered, [])
        saved = db.list_notification_outbox()[0]
        self.assertEqual(saved["status"], "delivery_unknown")
        self.assertEqual(saved["attempt_count"], 1)

    def test_per_monitor_lease_blocks_slotless_overlap_and_fences_old_owner(self):
        route_id = db.upsert_route("TPE", "KIX")
        monitor_id = db.add_monitor(route_id, "2026-09-01", 10000)["id"]
        first = db.create_check_run(monitor_id, trigger="manual")
        blocked = db.create_check_run(monitor_id, trigger="telegram")
        self.assertTrue(first["created"])
        self.assertFalse(blocked["created"])
        self.assertEqual(blocked["reason"], "monitor_busy")
        with self.assertRaises(PermissionError):
            db.finish_check_run(
                first["id"], status="succeeded", lease_token="wrong-owner"
            )
        db.finish_check_run(
            first["id"], status="succeeded", lease_token=first["lease_token"]
        )
        following = db.create_check_run(monitor_id, trigger="telegram")
        self.assertTrue(following["created"])

    def test_stale_slotless_run_is_closed_before_replacement(self):
        route_id = db.upsert_route("TPE", "NRT")
        monitor_id = db.add_monitor(route_id, "2026-09-01", 10000)["id"]
        stale_at = datetime.now().replace(microsecond=0) - timedelta(minutes=31)
        stale = db.create_check_run(monitor_id, trigger="manual", started_at=stale_at)
        replacement = db.create_check_run(
            monitor_id, trigger="telegram", started_at=stale_at + timedelta(minutes=31)
        )
        self.assertTrue(replacement["created"])
        self.assertNotEqual(replacement["id"], stale["id"])
        self.assertEqual(db.get_check_run(stale["id"])["status"], "failed")

    def test_startup_recovers_only_expired_runtime_leases(self):
        route_id = db.upsert_route("TPE", "NRT")
        monitor_id = db.add_monitor(route_id, "2026-09-01", 10000)["id"]
        stale_at = datetime.now().replace(microsecond=0) - timedelta(minutes=31)
        stale = db.create_check_run(monitor_id, trigger="manual", started_at=stale_at)

        db.init_db()

        recovered = db.get_check_run(stale["id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error_stage"], "startup_recovery")
        fresh = db.create_check_run(monitor_id, trigger="manual")
        self.assertTrue(fresh["created"])

    def test_outbox_final_send_guard_observes_monitor_pause(self):
        route_id = db.upsert_route("TPE", "HND")
        monitor_id = db.add_monitor(route_id, "2026-10-01", 9000)["id"]
        queued = db.enqueue_notification(
            channel="telegram",
            message="claimed fare",
            dedupe_key="final-send-fence",
            monitor_id=monitor_id,
        )
        claimed = db.claim_due_notifications()[0]
        self.assertTrue(
            db.outbox_delivery_is_owned(queued["id"], claimed["lease_token"])
        )

        db.set_monitor_active(monitor_id, False)

        self.assertFalse(
            db.outbox_delivery_is_owned(queued["id"], claimed["lease_token"])
        )

    def test_quota_reservation_is_atomic_and_reconciles_failed_attempt(self):
        now = datetime(2026, 8, 23, 10, 0)
        first = db.reserve_api_quota_request(
            "serpapi", "flight_search", 1, now=now
        )
        blocked = db.reserve_api_quota_request(
            "serpapi", "flight_search", 1, now=now
        )
        self.assertTrue(first["allowed"])
        self.assertFalse(blocked["allowed"])
        db.reconcile_api_quota_request(
            first["token"], attempted=True, success=False, detail="timeout", now=now
        )
        summary = db.get_api_usage_summary("serpapi", 1, now=now)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["reserved"], 0)

    def test_pause_and_policy_change_cancel_pending_outbox(self):
        route_id = db.upsert_route("TPE", "BKK")
        monitor_id = db.add_monitor(route_id, "2026-10-01", 9000)["id"]
        policy_run = db.create_check_run(monitor_id, trigger="manual")
        db.enqueue_notification(
            channel="telegram", message="old", dedupe_key="old-policy", monitor_id=monitor_id
        )
        db.update_monitor_policies(monitor_id, quota_mode="lean")
        self.assertEqual(db.list_notification_outbox()[0]["status"], "cancelled")
        self.assertEqual(db.get_check_run(policy_run["id"])["status"], "cancelled")
        with self.assertRaises(PermissionError):
            db.assert_monitor_run_lease(
                monitor_id, policy_run["id"], policy_run["lease_token"]
            )
        with self.assertRaises(PermissionError):
            db.add_price_record(
                route_id,
                "2026-10-01",
                8800,
                monitor_id=monitor_id,
                run_id=policy_run["id"],
                lease_token=policy_run["lease_token"],
            )
        with self.assertRaises(PermissionError):
            db.enqueue_notification(
                channel="telegram",
                message="stale result",
                dedupe_key="stale-policy-result",
                monitor_id=monitor_id,
                run_id=policy_run["id"],
                run_lease_token=policy_run["lease_token"],
            )
        previous_effective = db.get_monitor(monitor_id)["schedule_effective_at"]
        db.set_monitor_active(monitor_id, False)
        db.set_monitor_active(monitor_id, True)
        self.assertGreaterEqual(
            db.get_monitor(monitor_id)["schedule_effective_at"], previous_effective
        )

    def test_pause_marks_claimed_delivery_unknown_instead_of_retryable(self):
        route_id = db.upsert_route("TPE", "HND")
        monitor_id = db.add_monitor(route_id, "2026-10-01", 9000)["id"]
        queued = db.enqueue_notification(
            channel="telegram",
            message="possibly in flight",
            dedupe_key="pause-in-flight",
            monitor_id=monitor_id,
        )
        claimed = db.claim_due_notifications()
        self.assertEqual(claimed[0]["id"], queued["id"])

        db.set_monitor_active(monitor_id, False)

        saved = db.list_notification_outbox()[0]
        self.assertEqual(saved["status"], "delivery_unknown")
        self.assertIsNone(saved["lease_token"])

    def test_deferred_outbox_replaces_pending_digest_without_duplication(self):
        first_due = datetime.now().replace(microsecond=0) + timedelta(hours=1)
        second_due = first_due + timedelta(hours=1)
        first = db.enqueue_notification(
            channel="telegram",
            message="old digest",
            dedupe_key="digest-slot",
            next_attempt_at=first_due,
            replace_pending=True,
        )
        second = db.enqueue_notification(
            channel="telegram",
            message="latest digest",
            dedupe_key="digest-slot",
            next_attempt_at=second_due,
            replace_pending=True,
        )
        rows = db.list_notification_outbox()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "latest digest")
        self.assertEqual(rows[0]["next_attempt_at"], second_due.isoformat(timespec="seconds"))

    def test_monitor_policy_columns_round_trip(self):
        route_id = db.upsert_route("TPE", "NRT")
        created = db.add_monitor(
            route_id,
            "2026-11-01",
            15000,
            quota_mode="standard",
            airline_rules=[{"airline": "EVA", "target_price": 14500}],
            schedule_type="fixed_times",
            schedule_value="08:00,20:00",
            notification_policy="target_only",
            quiet_hours={"start": "23:00", "end": "07:00"},
        )
        monitor = db.get_monitor(created["id"])
        self.assertEqual(monitor["quota_mode"], "standard")
        self.assertEqual(monitor["schedule_value"], "08:00,20:00")
        self.assertEqual(monitor["notification_policy"], "target_only")

        db.update_monitor_policies(
            created["id"],
            target_price=13200,
            quota_mode="full",
        )
        updated = db.get_monitor(created["id"])
        self.assertEqual(updated["target_price"], 13200)
        self.assertEqual(updated["quota_mode"], "full")

        with self.assertRaises(ValueError):
            db.update_monitor_policies(created["id"], target_price=0)

    def test_api_quota_warning_is_claimed_once_per_period(self):
        self.assertTrue(db.claim_api_quota_warning("serpapi", "2026-08", 80))
        self.assertFalse(db.claim_api_quota_warning("serpapi", "2026-08", 80))
        db.release_api_quota_warning("serpapi", "2026-08", 80)
        self.assertTrue(db.claim_api_quota_warning("serpapi", "2026-08", 80))


if __name__ == "__main__":
    unittest.main()
