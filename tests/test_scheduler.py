import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import scheduler as scheduler_service
from src.policies import SchedulePolicy


class SchedulerSafetyTests(unittest.TestCase):
    @staticmethod
    def _fake_lock(acquired=True):
        lock = MagicMock()
        lock.acquire.return_value = acquired
        lock.owner.token = "test-owner-token"
        return lock

    def test_lifetime_lock_uses_process_identity_not_file_age(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_file = Path(temp_dir) / "scheduler.lock"
            first = scheduler_service.ProcessLock(lock_file, "scheduler")
            second = scheduler_service.ProcessLock(lock_file, "scheduler")
            self.assertTrue(first.acquire())
            # An old mtime must never evict a still-live owner.
            os.utime(lock_file, (1, 1))
            self.assertFalse(second.acquire())
            first.release()
            self.assertFalse(lock_file.exists())

    def test_windows_launcher_does_not_override_dotenv_schedule(self):
        launcher = (Path(scheduler_service.__file__).parent / "start_scheduler.bat").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("set SCHEDULE_HOURS=", launcher)
        self.assertNotIn("set CHECK_INTERVAL_MINUTES=", launcher)

    def test_status_heartbeat_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = Path(temp_dir) / "scheduler_status.json"
            with patch.object(scheduler_service, "STATUS_FILE", status_file):
                scheduler_service._write_status(state="healthy", last_result_count=1)
                payload = json.loads(status_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "healthy")
        self.assertEqual(payload["last_result_count"], 1)
        self.assertIn("updated_at", payload)

    def test_supervisor_tick_is_aligned_to_wall_clock(self):
        timezone = ZoneInfo("Asia/Taipei")
        before_noon = datetime(2026, 8, 23, 11, 59, 35, tzinfo=timezone)
        exact_boundary = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone)

        self.assertEqual(
            scheduler_service._next_aligned_supervisor_tick(before_noon, 5),
            datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone),
        )
        self.assertEqual(
            scheduler_service._next_aligned_supervisor_tick(exact_boundary, 5),
            datetime(2026, 8, 23, 12, 5, 0, tzinfo=timezone),
        )

    def test_status_writes_use_distinct_same_directory_temp_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = Path(temp_dir) / "scheduler_status.json"
            observed = []
            real_replace = os.replace

            def recording_replace(source, destination):
                observed.append(Path(source))
                real_replace(source, destination)

            with (
                patch.object(scheduler_service, "STATUS_FILE", status_file),
                patch.object(scheduler_service.os, "replace", side_effect=recording_replace),
            ):
                scheduler_service._write_status(state="running")
                scheduler_service._write_status(state="healthy")

            self.assertEqual(len(observed), 2)
            self.assertNotEqual(observed[0].name, observed[1].name)
            self.assertTrue(all(path.parent == status_file.parent for path in observed))
            self.assertEqual(list(status_file.parent.glob("*.tmp")), [])

    def test_duplicate_main_does_not_touch_database_or_heartbeat(self):
        fake_lock = self._fake_lock(acquired=False)
        with (
            patch.object(scheduler_service, "maintenance_active", return_value=False),
            patch.object(scheduler_service, "ProcessLock", return_value=fake_lock),
            patch.object(scheduler_service, "_run_owned_service") as owned,
            patch.object(scheduler_service, "_write_status") as write_status,
            patch.object(scheduler_service.db, "init_db") as init_db,
        ):
            exit_code = scheduler_service.main()
        self.assertEqual(exit_code, 2)
        owned.assert_not_called()
        write_status.assert_not_called()
        init_db.assert_not_called()
        fake_lock.release.assert_not_called()

    def test_main_never_waits_for_maintenance_while_holding_service_lock(self):
        first_lock = self._fake_lock()
        second_lock = self._fake_lock()
        with (
            patch.object(
                scheduler_service,
                "maintenance_active",
                side_effect=[True, False, True, False, False],
            ),
            patch.object(
                scheduler_service,
                "ProcessLock",
                side_effect=[first_lock, second_lock],
            ),
            patch.object(scheduler_service.time, "sleep") as sleep,
            patch.object(
                scheduler_service,
                "_run_owned_service",
                return_value=0,
            ) as owned,
        ):
            exit_code = scheduler_service.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 0.25])
        first_lock.release.assert_called_once_with()
        second_lock.release.assert_called_once_with()
        owned.assert_called_once_with(second_lock)

    def test_job_contention_does_not_overwrite_owner_heartbeat(self):
        fake_lock = self._fake_lock(acquired=False)
        with (
            patch.object(scheduler_service, "maintenance_active", return_value=False),
            patch.object(scheduler_service, "ProcessLock", return_value=fake_lock),
            patch.object(scheduler_service, "_write_status") as write_status,
            patch.object(scheduler_service, "check_all_monitors") as check,
        ):
            result = scheduler_service.job(scheduled_slot="2026-08-23T12:00+08:00")
        self.assertEqual(result, [])
        write_status.assert_not_called()
        check.assert_not_called()

    def test_console_encoding_cannot_abort_logging(self):
        class Cp950Console(io.StringIO):
            encoding = "cp950"

            def write(self, value):
                value.encode(self.encoding)
                return super().write(value)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            console = Cp950Console()
            with (
                patch.object(scheduler_service, "ROOT", root),
                patch("sys.stdout", console),
            ):
                scheduler_service.log_message("完成 ✅")
            contents = (root / "data" / "scheduler.log").read_text(encoding="utf-8")
        self.assertIn("完成 ✅", contents)

    def test_detect_missed_slots_uses_run_ledger(self):
        monitor = {
            "id": 9,
            "schedule_type": "fixed_times",
            "schedule_value": "00:00,12:00",
        }
        now = datetime(2026, 8, 12, 12, 5, tzinfo=ZoneInfo("Asia/Taipei"))
        with patch.object(scheduler_service.db, "slot_has_run", side_effect=lambda _id, slot: slot.endswith("00:00+08:00")):
            missing = scheduler_service.detect_missed_slots(
                monitor,
                SchedulePolicy.from_dict({}),
                now=now,
                lookback_hours=13,
            )
        self.assertEqual([slot.strftime("%H:%M") for slot in missing], ["12:00"])

    def test_legacy_last_checked_prevents_duplicate_restart_backfill(self):
        monitor = {
            "id": 9,
            "schedule_type": "fixed_times",
            "schedule_value": "00:00,12:00",
            "last_checked": "2026-08-12T12:00:12+08:00",
            "last_run_id": None,
        }
        now = datetime(2026, 8, 12, 12, 1, tzinfo=ZoneInfo("Asia/Taipei"))
        with patch.object(scheduler_service.db, "slot_has_run", return_value=False):
            missing = scheduler_service.detect_missed_slots(
                monitor,
                SchedulePolicy.from_dict({}),
                now=now,
                lookback_hours=13,
            )
        self.assertEqual(missing, [])

    def test_manual_check_does_not_mask_a_missed_scheduled_slot(self):
        monitor = {
            "id": 9,
            "schedule_type": "fixed_times",
            "schedule_value": "00:00,12:00",
            "last_checked": "2026-08-12T12:05:00+08:00",
            "last_run_id": "manual-run-id",
        }
        now = datetime(2026, 8, 12, 12, 10, tzinfo=ZoneInfo("Asia/Taipei"))
        with patch.object(scheduler_service.db, "slot_has_run", return_value=False):
            missing = scheduler_service.detect_missed_slots(
                monitor,
                SchedulePolicy.from_dict({}),
                now=now,
                lookback_hours=1,
            )
        self.assertEqual([slot.strftime("%H:%M") for slot in missing], ["12:00"])

    def test_new_monitor_does_not_backfill_slots_before_schedule_effective_time(self):
        monitor = {
            "id": 9,
            "schedule_type": "fixed_times",
            "schedule_value": "00:00,12:00",
            "created_at": "2026-08-13T15:00:00+08:00",
            "schedule_effective_at": "2026-08-13T15:00:00+08:00",
            "next_run_at": "2026-08-14T00:00:00+08:00",
        }
        same_day = datetime(2026, 8, 13, 15, 5, tzinfo=ZoneInfo("Asia/Taipei"))
        next_day = datetime(2026, 8, 14, 0, 5, tzinfo=ZoneInfo("Asia/Taipei"))
        with patch.object(scheduler_service.db, "slot_has_run", return_value=False):
            self.assertEqual(
                scheduler_service.detect_missed_slots(
                    monitor,
                    SchedulePolicy.from_dict({}),
                    now=same_day,
                    lookback_hours=24,
                ),
                [],
            )
            due = scheduler_service.detect_missed_slots(
                monitor,
                SchedulePolicy.from_dict({}),
                now=next_day,
                lookback_hours=24,
            )
        self.assertEqual([slot.strftime("%m-%d %H:%M") for slot in due], ["08-14 00:00"])

    def test_recover_missed_runs_backfills_only_latest_slot(self):
        monitor = {
            "id": 7,
            "origin": "TPE",
            "destination": "KIX",
            "schedule_type": "fixed_times",
            "schedule_value": "00:00,12:00",
        }
        now = datetime(2026, 8, 12, 12, 30, tzinfo=ZoneInfo("Asia/Taipei"))
        with (
            patch.object(scheduler_service.db, "list_monitors", return_value=[monitor]),
            patch.object(scheduler_service.db, "slot_has_run", return_value=False),
            patch.object(
                scheduler_service.db,
                "create_check_run",
                return_value={"id": "missed", "created": False},
            ),
            patch.object(scheduler_service.db, "set_monitor_next_run"),
            patch.object(scheduler_service, "_send_scheduler_incident") as incident,
            patch.object(
                scheduler_service,
                "check_all_monitors",
                return_value=[{"monitor_id": 7, "ok": True}],
            ) as check,
        ):
            results = scheduler_service.recover_missed_runs(
                SchedulePolicy.from_dict({}), now=now
            )
        self.assertEqual(results[0]["monitor_id"], 7)
        self.assertTrue(check.call_args.kwargs["backfill"])
        self.assertEqual(check.call_args.kwargs["monitor_ids"], [7])
        self.assertEqual(incident.call_count, 2)

    def test_api_failure_sends_incident_retries_once_and_reports_recovery(self):
        monitor = {"id": 7, "origin": "TPE", "destination": "KIX"}
        with (
            patch.dict(
                os.environ,
                {
                    "SCHEDULER_SHORT_RETRIES": "1",
                    "SCHEDULER_RETRY_DELAY_SECONDS": "1",
                },
            ),
            patch.object(
                scheduler_service,
                "check_all_monitors",
                side_effect=[
                    [{"monitor_id": 7, "ok": False, "error": "provider timeout"}],
                    [{"monitor_id": 7, "ok": True, "notified": True}],
                ],
            ) as check,
            patch.object(scheduler_service.time, "sleep") as sleep,
            patch.object(scheduler_service, "_send_scheduler_incident") as incident,
        ):
            results = scheduler_service._check_monitor_with_retry(
                monitor,
                scheduled_slot="2026-08-23T12:00+08:00",
                backfill=False,
            )

        self.assertTrue(results[0]["ok"])
        self.assertEqual(check.call_count, 2)
        self.assertEqual(
            check.call_args_list[1].kwargs["scheduled_slot"],
            "2026-08-23T12:00+08:00#retry-1",
        )
        sleep.assert_called_once_with(1.0)
        self.assertEqual(incident.call_count, 2)
        self.assertIn("pipeline-failed", incident.call_args_list[0].args[2])
        self.assertIn("pipeline-recovered", incident.call_args_list[1].args[2])

    def test_retry_budget_is_hard_capped_and_exhaustion_is_not_recovered(self):
        monitor = {"id": 7, "origin": "TPE", "destination": "KIX"}
        failed = [{"monitor_id": 7, "ok": False, "error": "upstream unavailable"}]
        with (
            patch.dict(
                os.environ,
                {
                    "SCHEDULER_SHORT_RETRIES": "99",
                    "SCHEDULER_RETRY_DELAY_SECONDS": "999",
                },
            ),
            patch.object(
                scheduler_service,
                "check_all_monitors",
                side_effect=[failed, failed, failed],
            ) as check,
            patch.object(scheduler_service.time, "sleep") as sleep,
            patch.object(scheduler_service, "_send_scheduler_incident") as incident,
        ):
            results = scheduler_service._check_monitor_with_retry(
                monitor,
                scheduled_slot="2026-08-23T12:00+08:00",
                backfill=False,
            )

        self.assertFalse(results[0]["ok"])
        self.assertEqual(check.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [120.0, 120.0])
        self.assertEqual(incident.call_count, 2)
        self.assertIn("pipeline-exhausted", incident.call_args_list[-1].args[2])
        self.assertFalse(any("recovered" in call.args[2] for call in incident.call_args_list))

    def test_quota_guard_skip_is_not_retried_or_reported_as_pipeline_failure(self):
        monitor = {"id": 7, "origin": "TPE", "destination": "KIX"}
        with (
            patch.object(
                scheduler_service,
                "check_all_monitors",
                return_value=[
                    {
                        "monitor_id": 7,
                        "ok": False,
                        "skipped": True,
                        "error_stage": "quota_guard",
                    }
                ],
            ) as check,
            patch.object(scheduler_service.time, "sleep") as sleep,
            patch.object(scheduler_service, "_send_scheduler_incident") as incident,
        ):
            scheduler_service._check_monitor_with_retry(
                monitor,
                scheduled_slot="2026-08-23T12:00+08:00",
                backfill=False,
            )
        check.assert_called_once()
        sleep.assert_not_called()
        incident.assert_not_called()

    def test_supervisor_heartbeats_before_and_during_monitor_batch(self):
        fake_lock = self._fake_lock()

        def recover(_policy, *, heartbeat):
            heartbeat("monitor_batch", monitor_id=7, batch_index=1, batch_total=1)
            heartbeat("monitor_attempt", monitor_id=7, attempt=1, total_attempts=2)
            return [{"monitor_id": 7, "ok": True}]

        with (
            patch.object(scheduler_service, "maintenance_active", return_value=False),
            patch.object(scheduler_service, "ProcessLock", return_value=fake_lock),
            patch.object(scheduler_service, "process_notification_outbox", return_value=[]),
            patch.object(scheduler_service, "recover_missed_runs", side_effect=recover),
            patch.object(
                scheduler_service,
                "_maybe_create_auto_backup",
                return_value={"state": "not_due"},
            ),
            patch.object(
                scheduler_service,
                "_next_due_monitor_time",
                return_value=datetime(2026, 8, 24, tzinfo=ZoneInfo("Asia/Taipei")),
            ),
            patch.object(scheduler_service, "_write_status") as write_status,
        ):
            scheduler_service._supervisor_tick(
                SchedulePolicy.from_dict({}), owner_token="owner-a"
            )

        phases = [
            call.kwargs.get("supervisor_phase")
            for call in write_status.call_args_list
            if call.kwargs.get("supervisor_phase")
        ]
        self.assertEqual(phases[0], "starting")
        self.assertIn("monitor_batch", phases)
        self.assertIn("monitor_attempt", phases)
        self.assertEqual(phases[-1], "waiting")
        self.assertTrue(
            all(
                call.kwargs.get("owner_token") == "owner-a"
                for call in write_status.call_args_list
            )
        )

    def test_supervisor_maintenance_or_lock_contention_never_reports_recovery(self):
        policy = SchedulePolicy.from_dict({})
        with (
            patch.object(scheduler_service, "maintenance_active", return_value=True),
            patch.object(scheduler_service, "ProcessLock") as lock,
            patch.object(scheduler_service, "_write_status") as write_status,
            patch.object(scheduler_service, "_send_scheduler_incident") as incident,
        ):
            scheduler_service._supervisor_tick(policy, owner_token="owner-a")
        lock.assert_not_called()
        write_status.assert_not_called()
        incident.assert_not_called()

        fake_lock = self._fake_lock(acquired=False)
        with (
            patch.object(scheduler_service, "maintenance_active", return_value=False),
            patch.object(scheduler_service, "ProcessLock", return_value=fake_lock),
            patch.object(scheduler_service, "_write_status") as write_status,
            patch.object(scheduler_service, "_send_scheduler_incident") as incident,
        ):
            scheduler_service._supervisor_tick(policy, owner_token="owner-a")
        write_status.assert_not_called()
        incident.assert_not_called()

    def test_auto_backup_is_daily_idempotent_after_0330(self):
        now = datetime(2026, 8, 23, 3, 31, tzinfo=ZoneInfo("Asia/Taipei"))
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir) / "backups"
            database = Path(temp_dir) / "flights.db"

            def create(destination, db_path):
                Path(destination).write_bytes(b"valid archive")
                return {"path": Path(destination)}

            with (
                patch.dict(
                    os.environ,
                    {
                        "AUTO_BACKUP_ENABLED": "true",
                        "AUTO_BACKUP_HOUR": "03:30",
                        "AUTO_BACKUP_RETENTION_DAYS": "14",
                    },
                ),
                patch.object(
                    scheduler_service,
                    "create_full_backup",
                    side_effect=create,
                ) as create_backup,
                patch.object(scheduler_service, "inspect_backup", return_value={}),
            ):
                first = scheduler_service._maybe_create_auto_backup(
                    now=now,
                    backup_dir=backup_dir,
                    db_path=database,
                )
                second = scheduler_service._maybe_create_auto_backup(
                    now=now,
                    backup_dir=backup_dir,
                    db_path=database,
                )

        self.assertEqual(first["state"], "created")
        self.assertEqual(second["state"], "current")
        create_backup.assert_called_once()
        self.assertEqual(create_backup.call_args.args[1], database)

    def test_auto_backup_retention_keeps_window_and_latest_valid_archive(self):
        now = datetime(2026, 8, 23, 4, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir)
            old = backup_dir / "skywatch-auto-20260801.zip"
            cutoff = backup_dir / "skywatch-auto-20260810.zip"
            newest = backup_dir / "skywatch-auto-20260823.zip"
            unrelated = backup_dir / "manual-export.zip"
            for path in (old, cutoff, newest, unrelated):
                path.write_bytes(b"archive")
            with patch.object(scheduler_service, "inspect_backup", return_value={}):
                removed = scheduler_service._prune_auto_backups(
                    backup_dir,
                    current=now,
                    retention_days=14,
                    newest_valid=newest,
                )

            self.assertEqual(removed, [old])
            self.assertFalse(old.exists())
            self.assertTrue(cutoff.exists())
            self.assertTrue(newest.exists())
            self.assertTrue(unrelated.exists())

    def test_failed_auto_backup_does_not_prune_latest_valid_backup(self):
        now = datetime(2026, 8, 23, 4, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir) / "backups"
            backup_dir.mkdir()
            latest_valid = backup_dir / "skywatch-auto-20260822.zip"
            latest_valid.write_bytes(b"last good archive")
            with (
                patch.dict(
                    os.environ,
                    {
                        "AUTO_BACKUP_ENABLED": "true",
                        "AUTO_BACKUP_HOUR": "03:30",
                    },
                ),
                patch.object(
                    scheduler_service,
                    "create_full_backup",
                    side_effect=OSError("disk full"),
                ),
                patch.object(scheduler_service, "_prune_auto_backups") as prune,
            ):
                with self.assertRaises(OSError):
                    scheduler_service._maybe_create_auto_backup(
                        now=now,
                        backup_dir=backup_dir,
                        db_path=Path(temp_dir) / "flights.db",
                    )
            prune.assert_not_called()
            self.assertTrue(latest_valid.exists())

    def test_auto_backup_failure_updates_status_and_sends_daily_incident(self):
        fake_lock = self._fake_lock()
        with (
            patch.object(scheduler_service, "maintenance_active", return_value=False),
            patch.object(scheduler_service, "ProcessLock", return_value=fake_lock),
            patch.object(scheduler_service, "process_notification_outbox", return_value=[]),
            patch.object(scheduler_service, "recover_missed_runs", return_value=[]),
            patch.object(
                scheduler_service,
                "_maybe_create_auto_backup",
                side_effect=OSError("disk full"),
            ),
            patch.object(
                scheduler_service,
                "_next_due_monitor_time",
                return_value=datetime(2026, 8, 24, tzinfo=ZoneInfo("Asia/Taipei")),
            ),
            patch.object(scheduler_service, "_write_status") as write_status,
            patch.object(scheduler_service, "_send_scheduler_incident") as incident,
        ):
            scheduler_service._supervisor_tick(
                SchedulePolicy.from_dict({}), owner_token="owner-a"
            )

        self.assertTrue(
            any(
                call.kwargs.get("auto_backup_state") == "error"
                for call in write_status.call_args_list
            )
        )
        self.assertTrue(any("auto-backup:" in call.args[2] for call in incident.call_args_list))

    def test_global_job_excludes_custom_schedule_monitors(self):
        global_monitor = {
            "id": 1,
            "schedule_type": "global",
            "schedule_value": "",
        }
        custom_monitor = {
            "id": 2,
            "schedule_type": "daily",
            "schedule_value": "08:00",
        }
        fallback = SchedulePolicy.from_dict(
            {"mode": "fixed_times", "times": "00:00,12:00"}
        )
        with (
            patch.object(
                scheduler_service,
                "ProcessLock",
                return_value=self._fake_lock(),
            ),
            patch.object(scheduler_service, "maintenance_active", return_value=False),
            patch.object(scheduler_service, "_write_status"),
            patch.object(scheduler_service, "log_message"),
            patch.object(
                scheduler_service.db,
                "list_monitors",
                return_value=[global_monitor, custom_monitor],
            ),
            patch.object(scheduler_service, "_persist_next_monitor_run"),
            patch.object(scheduler_service, "check_all_monitors", return_value=[]) as check,
        ):
            scheduler_service.job(
                scheduled_slot="2026-08-12T12:00+08:00",
                fallback_policy=fallback,
            )
        self.assertEqual(check.call_args.kwargs["monitor_ids"], [1])

    def test_supervisor_dispatches_recent_custom_slot_without_false_incident(self):
        monitor = {
            "id": 8,
            "origin": "TPE",
            "destination": "NRT",
            "schedule_type": "daily",
            "schedule_value": "12:00",
        }
        now = datetime(2026, 8, 12, 12, 5, tzinfo=ZoneInfo("Asia/Taipei"))
        with (
            patch.object(scheduler_service.db, "list_monitors", return_value=[monitor]),
            patch.object(scheduler_service.db, "slot_has_run", return_value=False),
            patch.object(scheduler_service.db, "set_monitor_next_run"),
            patch.object(scheduler_service, "_send_scheduler_incident") as incident,
            patch.object(
                scheduler_service,
                "check_all_monitors",
                return_value=[{"monitor_id": 8, "ok": True}],
            ) as check,
        ):
            scheduler_service.recover_missed_runs(
                SchedulePolicy.from_dict({}), now=now
            )
        self.assertFalse(check.call_args.kwargs["backfill"])
        incident.assert_not_called()

    def test_interval_slots_keep_stable_created_at_anchor(self):
        monitor = {
            "id": 9,
            "created_at": "2026-08-12T00:00:00",
            "schedule_type": "interval",
            "schedule_value": "60",
            "next_run_at": None,
        }
        policy = SchedulePolicy.from_dict({})
        first_now = datetime(2026, 8, 12, 3, 5, tzinfo=ZoneInfo("Asia/Taipei"))
        second_now = datetime(2026, 8, 12, 3, 10, tzinfo=ZoneInfo("Asia/Taipei"))
        with patch.object(scheduler_service.db, "slot_has_run", return_value=False):
            first = scheduler_service.detect_missed_slots(
                monitor, policy, now=first_now, lookback_hours=3
            )
            second = scheduler_service.detect_missed_slots(
                monitor, policy, now=second_now, lookback_hours=3
            )
        self.assertEqual(
            [_slot.strftime("%H:%M") for _slot in first], ["03:00", "02:00", "01:00"]
        )
        self.assertEqual(
            [_slot.strftime("%H:%M") for _slot in second], ["03:00", "02:00", "01:00"]
        )

    def test_interval_mode_has_only_one_durable_dispatch_owner(self):
        fake_scheduler = MagicMock()
        fake_lock = self._fake_lock()
        now = datetime(2026, 8, 23, 11, 59, 35, tzinfo=ZoneInfo("Asia/Taipei"))
        with (
            patch.object(scheduler_service, "BlockingScheduler", return_value=fake_scheduler),
            patch.object(scheduler_service, "ProcessLock", return_value=fake_lock),
            patch.object(scheduler_service, "maintenance_active", return_value=False),
            patch.object(scheduler_service.db, "init_db"),
            patch.object(scheduler_service, "_now", return_value=now),
            patch.object(scheduler_service, "_supervisor_tick") as startup_tick,
            patch.object(scheduler_service, "log_message"),
            patch.object(scheduler_service, "_write_status"),
            patch.dict(
                os.environ,
                {"SCHEDULE_HOURS": "", "CHECK_INTERVAL_MINUTES": "60"},
            ),
        ):
            exit_code = scheduler_service.main()
        self.assertEqual(exit_code, 0)
        fake_lock.release.assert_called_once_with()
        job_ids = [call.kwargs.get("id") for call in fake_scheduler.add_job.call_args_list]
        self.assertNotIn("flight_check_interval", job_ids)
        self.assertIn("scheduler_supervisor", job_ids)
        startup_tick.assert_called_once()
        supervisor_job = next(
            call
            for call in fake_scheduler.add_job.call_args_list
            if call.kwargs.get("id") == "scheduler_supervisor"
        )
        self.assertEqual(
            supervisor_job.kwargs["next_run_time"],
            datetime(2026, 8, 23, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
        )

    def test_fixed_mode_has_only_one_durable_dispatch_owner_after_sleep(self):
        fake_scheduler = MagicMock()
        fake_lock = self._fake_lock()
        with (
            patch.object(scheduler_service, "BlockingScheduler", return_value=fake_scheduler),
            patch.object(scheduler_service, "ProcessLock", return_value=fake_lock),
            patch.object(scheduler_service, "maintenance_active", return_value=False),
            patch.object(scheduler_service.db, "init_db"),
            patch.object(scheduler_service, "_supervisor_tick") as startup_tick,
            patch.object(scheduler_service, "log_message"),
            patch.object(scheduler_service, "_write_status"),
            patch.dict(
                os.environ,
                {"SCHEDULE_HOURS": "0,12", "CHECK_INTERVAL_MINUTES": ""},
            ),
        ):
            exit_code = scheduler_service.main()
        self.assertEqual(exit_code, 0)
        fake_lock.release.assert_called_once_with()
        job_ids = [call.kwargs.get("id") for call in fake_scheduler.add_job.call_args_list]
        self.assertNotIn("flight_check_cron", job_ids)
        self.assertIn("scheduler_supervisor", job_ids)
        startup_tick.assert_called_once()


if __name__ == "__main__":
    unittest.main()
