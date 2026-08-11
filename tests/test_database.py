import sqlite3
import tempfile
import unittest
from datetime import datetime
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

    def test_api_usage_counts_successful_quota_calls(self):
        stamp = datetime(2026, 8, 11, 9, 0)
        db.record_api_usage("serpapi", "flight_search", True, created_at=stamp)
        db.record_api_usage("serpapi", "flight_search", False, created_at=stamp)
        summary = db.get_api_usage_summary("serpapi", 250, now=stamp)
        self.assertEqual(summary["used"], 1)
        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["remaining"], 249)

    def test_api_quota_warning_is_claimed_once_per_period(self):
        self.assertTrue(db.claim_api_quota_warning("serpapi", "2026-08", 80))
        self.assertFalse(db.claim_api_quota_warning("serpapi", "2026-08", 80))
        db.release_api_quota_warning("serpapi", "2026-08", 80)
        self.assertTrue(db.claim_api_quota_warning("serpapi", "2026-08", 80))


if __name__ == "__main__":
    unittest.main()
