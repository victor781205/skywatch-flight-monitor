import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

from src import backup
from src import database as db
from src.runtime_lock import ProcessLock


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = self.root / "data" / "flights.db"
        self.original_path = db.DB_PATH
        db.DB_PATH = self.database
        db.init_db()
        self.route_id = db.upsert_route("TPE", "NRT", "Taipei", "Tokyo")
        self.monitor_id = db.add_monitor(
            self.route_id,
            "2026-10-01",
            10000,
            return_date="2026-10-08",
            adults=2,
            travel_class=3,
            trip_type="round_trip",
        )["id"]

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def _target_price(self, database=None):
        target = database or self.database
        with closing(sqlite3.connect(target)) as conn:
            return conn.execute("SELECT target_price FROM monitors ORDER BY id LIMIT 1").fetchone()[0]

    def _set_target_price(self, value):
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("UPDATE monitors SET target_price=?", (value,))
            conn.commit()

    def test_sqlite_snapshot_includes_uncheckpointed_wal_data(self):
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE monitors SET target_price=8765")
            conn.commit()
        snapshot = backup.snapshot_database(self.database, self.root / "snapshot.sqlite")
        self.assertEqual(self._target_price(snapshot), 8765)
        with closing(sqlite3.connect(snapshot)) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_monitor_export_has_schema_preview_and_no_credentials(self):
        destination = self.root / "monitors.json"
        report = backup.export_monitors(destination, self.database)
        document = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], backup.MONITOR_SCHEMA_VERSION)
        self.assertEqual(document["preview"]["monitor_count"], 1)
        self.assertEqual(document["monitors"][0]["origin"], "TPE")
        self.assertEqual(report["preview"]["active_count"], 1)
        lower = destination.read_text(encoding="utf-8").lower()
        self.assertNotIn("telegram_bot_token", lower)
        self.assertNotIn("serpapi_api_key", lower)

    def test_monitor_import_preview_and_conflict_policies(self):
        exported = self.root / "monitors.json"
        backup.export_monitors(exported, self.database)
        document = json.loads(exported.read_text(encoding="utf-8"))
        document["monitors"][0]["target_price"] = 7654
        exported.write_text(json.dumps(document), encoding="utf-8")

        preview = backup.preview_monitor_import(exported, self.database)
        self.assertEqual(preview["conflict_count"], 1)
        skipped = backup.import_monitors(exported, self.database, conflict="skip")
        self.assertEqual(skipped["skipped"], 1)
        self.assertEqual(self._target_price(), 10000)
        self.assertTrue(skipped["restore_point"].is_file())

        updated = backup.import_monitors(exported, self.database, conflict="update")
        self.assertEqual(updated["updated"], 1)
        self.assertEqual(self._target_price(), 7654)

        duplicated = backup.import_monitors(exported, self.database, conflict="duplicate")
        self.assertEqual(duplicated["created"], 1)
        self.assertEqual(duplicated["duplicates"], 1)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM monitors").fetchone()[0], 2)

    def test_import_validation_happens_before_database_changes(self):
        source = self.root / "invalid.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": backup.MONITOR_SCHEMA_VERSION,
                    "monitors": [
                        {
                            "origin": "../../.env",
                            "destination": "NRT",
                            "outbound_date": "2026-10-01",
                            "target_price": 1,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(backup.BackupValidationError):
            backup.import_monitors(source, self.database)
        self.assertEqual(self._target_price(), 10000)
        self.assertFalse((self.database.parent / "backups").exists())

    def test_full_backup_has_strict_credential_free_allowlist(self):
        (self.root / ".env").write_text("TELEGRAM_BOT_TOKEN=do-not-export", encoding="utf-8")
        archive_path = self.root / "skywatch-backup.zip"
        report = backup.create_full_backup(archive_path, self.database)
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(set(archive.namelist()), backup.ARCHIVE_MEMBERS)
            all_names = " ".join(archive.namelist()).lower()
            self.assertNotIn(".env", all_names)
            self.assertNotIn("token", all_names)
            self.assertNotIn("secret", all_names)
            self.assertNotIn(b"do-not-export", b"".join(archive.read(name) for name in archive.namelist()))
        inspected = backup.inspect_backup(archive_path)
        self.assertEqual(inspected["preview"]["monitor_count"], 1)
        self.assertEqual(report["preview"]["route_count"], 1)
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(
            str(manifest["database_schema_version"]),
            str(db.DATABASE_SCHEMA_VERSION),
        )

    def test_archive_path_traversal_is_rejected(self):
        valid = self.root / "valid.zip"
        backup.create_full_backup(valid, self.database)
        malicious = self.root / "malicious.zip"
        with zipfile.ZipFile(valid) as source, zipfile.ZipFile(malicious, "w") as target:
            for name in source.namelist():
                target.writestr(name, source.read(name))
            target.writestr("../.env", "SECRET=stolen")
        with self.assertRaises(backup.BackupValidationError):
            backup.inspect_backup(malicious)
        with self.assertRaises(backup.BackupValidationError):
            backup.restore_full_backup(malicious, self.database)

    def test_checksum_tampering_is_rejected_before_restore(self):
        valid = self.root / "valid.zip"
        backup.create_full_backup(valid, self.database)
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(valid) as source, zipfile.ZipFile(tampered, "w") as target:
            for name in source.namelist():
                content = source.read(name)
                if name == "monitors.json":
                    content += b" "
                target.writestr(name, content)
        self._set_target_price(5432)
        with self.assertRaises(backup.BackupValidationError):
            backup.restore_full_backup(tampered, self.database)
        self.assertEqual(self._target_price(), 5432)

    def test_restore_creates_pre_restore_snapshot_and_replaces_atomically(self):
        archive_path = self.root / "before-change.zip"
        backup.create_full_backup(archive_path, self.database)
        self._set_target_price(22222)

        report = backup.restore_full_backup(archive_path, self.database)

        self.assertEqual(self._target_price(), 10000)
        restore_point = report["restore_point"]
        self.assertTrue(restore_point.is_file())
        self.assertEqual(self._target_price(restore_point), 22222)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_post_replace_failure_rolls_back_to_pre_restore_snapshot(self):
        archive_path = self.root / "before-change.zip"
        backup.create_full_backup(archive_path, self.database)
        self._set_target_price(33333)
        real_validate = backup._validate_sqlite
        failed_live_validation = False
        resolved_live_database = self.database.resolve()

        def fail_first_live_validation(path):
            nonlocal failed_live_validation
            # Windows hosted runners can expose the temp directory through a
            # short-name or junction spelling. Compare canonical paths just as
            # restore_full_backup does before invoking the validator.
            if Path(path).resolve() == resolved_live_database and not failed_live_validation:
                failed_live_validation = True
                raise backup.BackupValidationError("simulated post-replace failure")
            return real_validate(path)

        with mock.patch.object(backup, "_validate_sqlite", side_effect=fail_first_live_validation):
            with self.assertRaises(backup.BackupRestoreError):
                backup.restore_full_backup(archive_path, self.database)

        self.assertTrue(failed_live_validation)
        self.assertEqual(self._target_price(), 33333)

    def test_restore_rejects_active_worker_without_changing_database(self):
        archive_path = self.root / "before-change.zip"
        backup.create_full_backup(archive_path, self.database)
        self._set_target_price(44444)
        worker = ProcessLock(self.database.parent / "scheduler.lock", "scheduler")
        self.assertTrue(worker.acquire())
        try:
            with self.assertRaisesRegex(backup.BackupRestoreError, "Stop active workers"):
                backup.restore_full_backup(archive_path, self.database)
        finally:
            worker.release()
        self.assertEqual(self._target_price(), 44444)
        self.assertFalse((self.database.parent / "maintenance.lock").exists())

    def test_restore_rejects_database_schema_mismatch(self):
        archive_path = self.root / "before-change.zip"
        backup.create_full_backup(archive_path, self.database)
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute(
                "UPDATE schema_meta SET value='999' WHERE key='schema_version'"
            )
            conn.commit()
        with self.assertRaisesRegex(backup.BackupRestoreError, "schema mismatch"):
            backup.restore_full_backup(archive_path, self.database)

    def test_restore_quarantines_historical_work_and_resets_schedule(self):
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute(
                "UPDATE monitors SET next_run_at='2026-10-01T00:00:00Z' WHERE id=?",
                (self.monitor_id,),
            )
            conn.execute(
                """
                INSERT INTO check_runs
                    (id, monitor_id, trigger, status, started_at, created_at)
                VALUES ('old-run', ?, 'scheduled', 'running', '2026-01-01T00:00:00Z',
                        '2026-01-01T00:00:00Z')
                """,
                (self.monitor_id,),
            )
            conn.execute(
                """
                INSERT INTO notification_outbox
                    (run_id, monitor_id, channel, message, status, next_attempt_at,
                     dedupe_key, created_at, updated_at)
                VALUES ('old-run', ?, 'telegram', 'old quote', 'pending',
                        '2026-01-01T00:00:00Z', 'old-message',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (self.monitor_id,),
            )
            conn.commit()
        archive_path = self.root / "historical.zip"
        backup.create_full_backup(archive_path, self.database)

        backup.restore_full_backup(archive_path, self.database)

        with closing(sqlite3.connect(self.database)) as conn:
            run_status = conn.execute(
                "SELECT status, error_stage FROM check_runs WHERE id='old-run'"
            ).fetchone()
            message_status = conn.execute(
                "SELECT status FROM notification_outbox WHERE dedupe_key='old-message'"
            ).fetchone()[0]
            next_run, effective_at = conn.execute(
                "SELECT next_run_at, schedule_effective_at FROM monitors WHERE id=?",
                (self.monitor_id,),
            ).fetchone()
        self.assertEqual(run_status, ("failed", "restore"))
        self.assertEqual(message_status, "cancelled")
        self.assertIsNone(next_run)
        self.assertTrue(effective_at)

    def test_bad_conflict_policy_is_rejected_before_snapshot(self):
        exported = self.root / "monitors.json"
        backup.export_monitors(exported, self.database)
        with self.assertRaises(backup.BackupValidationError):
            backup.import_monitors(exported, self.database, conflict="overwrite")
        self.assertFalse((self.database.parent / "backups").exists())


if __name__ == "__main__":
    unittest.main()
