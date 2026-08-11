import tempfile
import unittest
import json
import io
from pathlib import Path
from unittest.mock import patch

import scheduler as scheduler_service
from src.scheduler import get_scheduler


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


if __name__ == "__main__":
    unittest.main()
