from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.runtime_lock import ProcessLock, lock_owner_alive, maintenance_active, parse_lock_owner


class RuntimeLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_process_lock_has_token_and_blocks_second_owner(self) -> None:
        path = self.root / "scheduler.lock"
        first = ProcessLock(path, "scheduler")
        second = ProcessLock(path, "scheduler")

        self.assertTrue(first.acquire())
        owner = parse_lock_owner(path.read_bytes())
        self.assertIsNotNone(owner)
        self.assertEqual(owner.pid, os.getpid())
        self.assertTrue(owner.token)
        self.assertFalse(second.acquire())

        first.release()
        self.assertFalse(path.exists())
        self.assertTrue(second.acquire())
        second.release()

    def test_stale_lock_is_recovered_but_live_pid_is_not(self) -> None:
        path = self.root / "telegram_bot.lock"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 999_999_999,
                    "token": "stale",
                    "fingerprint": "missing",
                    "service": "telegram",
                    "created_at": "2020-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        lock = ProcessLock(path, "telegram")
        self.assertTrue(lock.acquire())
        lock.release()

        live = {
            "version": 1,
            "pid": os.getpid(),
            "token": "other",
            "fingerprint": None,
            "service": "telegram",
            "created_at": "2020-01-01T00:00:00Z",
        }
        path.write_text(json.dumps(live), encoding="utf-8")
        self.assertTrue(lock_owner_alive(path))
        self.assertFalse(ProcessLock(path, "telegram").acquire())

    def test_fingerprint_mismatch_does_not_treat_reused_pid_as_owner(self) -> None:
        path = self.root / "scheduler.lock"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": os.getpid(),
                    "token": "old-process",
                    "fingerprint": "definitely-not-current",
                    "service": "scheduler",
                    "created_at": "2020-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(lock_owner_alive(path))
        replacement = ProcessLock(path, "scheduler")
        self.assertTrue(replacement.acquire())
        replacement.release()

    def test_maintenance_active_requires_a_live_owner(self) -> None:
        maintenance = ProcessLock(self.root / "maintenance.lock", "maintenance")
        self.assertFalse(maintenance_active(self.root))
        self.assertTrue(maintenance.acquire())
        self.assertTrue(maintenance_active(self.root))
        maintenance.release()
        self.assertFalse(maintenance_active(self.root))

    def test_release_never_deletes_another_owner_payload(self) -> None:
        path = self.root / "scheduler.lock"
        lock = ProcessLock(path, "scheduler")
        self.assertTrue(lock.acquire())
        replacement = b'{"pid":1,"token":"replacement"}\n'
        path.write_bytes(replacement)
        lock.release()
        self.assertEqual(path.read_bytes(), replacement)


if __name__ == "__main__":
    unittest.main()
