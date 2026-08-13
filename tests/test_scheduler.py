import tempfile
import unittest
import json
import io
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

import scheduler as scheduler_service
from src.scheduler import get_scheduler
from src.policies import SchedulePolicy


class SchedulerSafetyTests(unittest.TestCase):
    def test_job_lock_allows_only_one_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_file = Path(temp_dir) / "scheduler.lock"
            with patch.object(scheduler_service, "LOCK_FILE", lock_file):
                first = scheduler_service._acquire_job_lock()
                self.assertIsNotNone(first)
                self.assertIsNone(scheduler_service._acquire_job_lock())
                scheduler_service._release_job_lock(first)
                self.assertFalse(lock_file.exists())

    def test_legacy_in_process_scheduler_does_not_auto_start(self):
        self.assertFalse(get_scheduler().running)

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
            patch.object(scheduler_service, "_acquire_job_lock", return_value=10),
            patch.object(scheduler_service, "_release_job_lock"),
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
        with (
            patch.object(scheduler_service, "BlockingScheduler", return_value=fake_scheduler),
            patch.object(scheduler_service.db, "init_db"),
            patch.object(scheduler_service, "log_message"),
            patch.object(scheduler_service, "_write_status"),
            patch.dict(
                os.environ,
                {"SCHEDULE_HOURS": "", "CHECK_INTERVAL_MINUTES": "60"},
            ),
        ):
            scheduler_service.main()
        job_ids = [call.kwargs.get("id") for call in fake_scheduler.add_job.call_args_list]
        self.assertNotIn("flight_check_interval", job_ids)
        self.assertIn("scheduler_supervisor", job_ids)

    def test_fixed_mode_has_only_one_durable_dispatch_owner_after_sleep(self):
        fake_scheduler = MagicMock()
        with (
            patch.object(scheduler_service, "BlockingScheduler", return_value=fake_scheduler),
            patch.object(scheduler_service.db, "init_db"),
            patch.object(scheduler_service, "log_message"),
            patch.object(scheduler_service, "_write_status"),
            patch.dict(
                os.environ,
                {"SCHEDULE_HOURS": "0,12", "CHECK_INTERVAL_MINUTES": ""},
            ),
        ):
            scheduler_service.main()
        job_ids = [call.kwargs.get("id") for call in fake_scheduler.add_job.call_args_list]
        self.assertNotIn("flight_check_cron", job_ids)
        self.assertIn("scheduler_supervisor", job_ids)


if __name__ == "__main__":
    unittest.main()
