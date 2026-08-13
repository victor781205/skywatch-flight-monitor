import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.telegram_commands import TelegramCommandResult
from telegram_bot import (
    CommandCallbacks,
    OffsetStore,
    SchedulerHeartbeatWatchdog,
    SingleInstanceLock,
    TelegramAPI,
    TelegramAPIError,
    TelegramBotRunner,
    WatchdogEvent,
)


def telegram_update(update_id=10, text="/status", chat_id=123, *, is_bot=False):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 7,
            "text": text,
            "chat": {"id": chat_id},
            "from": {"id": 9, "username": "victor", "is_bot": is_bot},
        },
    }


class FakeAPI:
    def __init__(self, batches=None):
        self.batches = list(batches or [])
        self.get_calls = []
        self.send_calls = []
        self.send_error = None

    def get_updates(self, offset, *, timeout):
        self.get_calls.append((offset, timeout))
        if self.batches:
            batch = self.batches.pop(0)
            if isinstance(batch, Exception):
                raise batch
            return batch
        return []

    def send_message(self, chat_id, text):
        self.send_calls.append((chat_id, text))
        if self.send_error:
            raise self.send_error


class FakeRouter:
    def __init__(self, callback=None):
        self.updates = []
        self.callback = callback

    def handle_update(self, update):
        self.updates.append(update)
        if self.callback:
            self.callback(update)
        return TelegramCommandResult(handled=True, text="reply", command="status")


class FakeDatabase:
    def __init__(self, monitors=None, summary=None):
        self.monitors = list(monitors or [])
        self.summary = summary or {
            "limit": 250,
            "used": 12,
            "attempted": 15,
            "failed": 3,
            "remaining": 238,
        }

    def list_monitors(self, active_only=False):
        self.last_active_only = active_only
        return list(self.monitors)

    def get_api_usage_summary(self, provider, monthly_limit):
        self.last_summary_args = (provider, monthly_limit)
        return dict(self.summary)


class OffsetAndRunnerTests(unittest.TestCase):
    def test_offset_store_round_trip_is_monotonic(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "offset.json"
            store = OffsetStore(path)

            self.assertEqual(store.next_offset, 0)
            self.assertEqual(store.advance_to(11), 11)
            self.assertEqual(store.advance_to(3), 11)
            self.assertEqual(OffsetStore(path).next_offset, 11)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"next_offset": 11})

    def test_duplicate_update_is_routed_and_replied_once(self):
        with tempfile.TemporaryDirectory() as temp:
            update = telegram_update()
            api = FakeAPI([[update, dict(update)]])
            router = FakeRouter()
            runner = TelegramBotRunner(
                api,
                router,
                offset_store=OffsetStore(Path(temp) / "offset.json"),
            )

            processed = runner.poll_once()

            self.assertEqual(processed, 1)
            self.assertEqual(len(router.updates), 1)
            self.assertEqual(api.send_calls, [(123, "reply")])
            self.assertEqual(runner.offset_store.next_offset, 11)

    def test_offset_is_persisted_before_command_callback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "offset.json"
            observed = []
            router = FakeRouter(
                lambda _update: observed.append(
                    json.loads(path.read_text(encoding="utf-8"))["next_offset"]
                )
            )
            runner = TelegramBotRunner(
                FakeAPI([[telegram_update(update_id=42)]]),
                router,
                offset_store=OffsetStore(path),
            )

            runner.poll_once()

            self.assertEqual(observed, [43])

    def test_ambiguous_send_failure_is_not_retried_on_redelivery(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "offset.json"
            update = telegram_update(update_id=50)
            api = FakeAPI([[update], [update]])
            api.send_error = TelegramAPIError()
            router = FakeRouter()
            runner = TelegramBotRunner(api, router, offset_store=OffsetStore(path))

            with self.assertRaises(TelegramAPIError):
                runner.poll_once()
            self.assertEqual(OffsetStore(path).next_offset, 51)

            api.send_error = None
            self.assertEqual(runner.poll_once(), 0)
            self.assertEqual(len(api.send_calls), 1)
            self.assertEqual(len(router.updates), 1)

    def test_messages_sent_by_bots_are_ignored_but_acknowledged(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI([[telegram_update(is_bot=True)]])
            router = FakeRouter()
            runner = TelegramBotRunner(
                api,
                router,
                offset_store=OffsetStore(Path(temp) / "offset.json"),
            )

            self.assertEqual(runner.poll_once(), 1)
            self.assertEqual(router.updates, [])
            self.assertEqual(api.send_calls, [])

    def test_poll_failures_use_bounded_backoff_without_exception_details(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI([[telegram_update()]])
            api.get_updates = Mock(side_effect=[TelegramAPIError(retry_after=3), []])
            sleeps = []
            logs = []
            runner = TelegramBotRunner(
                api,
                FakeRouter(),
                offset_store=OffsetStore(Path(temp) / "offset.json"),
                max_backoff=10,
                sleep_func=sleeps.append,
                logger=logs.append,
            )

            runner.run_forever(max_cycles=2)

            self.assertEqual(sleeps, [3.0])
            self.assertEqual(len(logs), 1)
            self.assertNotIn("api.telegram.org", logs[0])

    def test_runner_sends_independent_scheduler_watchdog_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI([[]])
            watchdog = Mock()
            watchdog.poll.return_value = WatchdogEvent(
                "incident",
                "stale",
                "scheduler heartbeat stale",
                ("123",),
            )
            runner = TelegramBotRunner(
                api,
                FakeRouter(),
                offset_store=OffsetStore(Path(temp) / "offset.json"),
                heartbeat_watchdog=watchdog,
                alert_chat_ids={"123"},
            )
            self.assertEqual(runner.poll_once(), 0)
        self.assertEqual(api.send_calls, [("123", "scheduler heartbeat stale")])
        watchdog.poll.assert_called_once_with(("123",))
        watchdog.mark_delivered.assert_called_once()

    def test_watchdog_delivery_failure_retries_only_unsent_recipient(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "scheduler_status.json"
            state_path = root / "watchdog.json"
            status_path.write_text(
                json.dumps(
                    {
                        "state": "waiting",
                        "updated_at": "2026-08-12T12:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            watchdog = SchedulerHeartbeatWatchdog(
                status_path=status_path,
                state_path=state_path,
                stale_after_minutes=15,
                startup_grace_seconds=0,
                now_func=lambda: datetime(2026, 8, 12, 12, 20, tzinfo=timezone.utc),
                monotonic_func=lambda: 100.0,
            )
            first_event = watchdog.poll(("123", "456"))
            watchdog.mark_delivered(first_event, "123")
            retry_event = watchdog.poll(("123", "456"))

        self.assertEqual(first_event.recipients, ("123", "456"))
        self.assertEqual(retry_event.recipients, ("456",))

    def test_runner_continues_after_one_watchdog_recipient_fails(self):
        class SelectiveFailureAPI(FakeAPI):
            def __init__(self, batches=None, failed_chat_ids=()):
                super().__init__(batches)
                self.failed_chat_ids = {str(item) for item in failed_chat_ids}

            def send_message(self, chat_id, text):
                self.send_calls.append((str(chat_id), text))
                if str(chat_id) in self.failed_chat_ids:
                    raise TelegramAPIError(retry_after=1)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "scheduler_status.json"
            state_path = root / "watchdog.json"
            status_path.write_text(
                json.dumps(
                    {
                        "state": "waiting",
                        "updated_at": "2026-08-12T12:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            watchdog = SchedulerHeartbeatWatchdog(
                status_path=status_path,
                state_path=state_path,
                stale_after_minutes=15,
                startup_grace_seconds=0,
                now_func=lambda: datetime(2026, 8, 12, 12, 20, tzinfo=timezone.utc),
                monotonic_func=lambda: 100.0,
            )
            first_api = SelectiveFailureAPI([[]], failed_chat_ids={"123"})
            first_runner = TelegramBotRunner(
                first_api,
                FakeRouter(),
                offset_store=OffsetStore(root / "offset.json"),
                heartbeat_watchdog=watchdog,
                alert_chat_ids={"123", "456"},
            )
            with self.assertRaises(TelegramAPIError):
                first_runner.poll_once()
            state_after_failure = json.loads(state_path.read_text(encoding="utf-8"))

            retry_api = SelectiveFailureAPI([[]])
            retry_runner = TelegramBotRunner(
                retry_api,
                FakeRouter(),
                offset_store=OffsetStore(root / "offset.json"),
                heartbeat_watchdog=watchdog,
                alert_chat_ids={"123", "456"},
            )
            self.assertEqual(retry_runner.poll_once(), 0)

        self.assertEqual([chat for chat, _ in first_api.send_calls], ["123", "456"])
        self.assertEqual(state_after_failure["delivered"], ["456"])
        self.assertEqual([chat for chat, _ in retry_api.send_calls], ["123"])


class HeartbeatWatchdogTests(unittest.TestCase):
    def test_watchdog_alerts_once_then_reports_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "scheduler_status.json"
            state_path = root / "watchdog.json"
            current = [datetime(2026, 8, 12, 12, 20, tzinfo=timezone.utc)]
            status_path.write_text(
                json.dumps(
                    {
                        "state": "waiting",
                        "updated_at": "2026-08-12T12:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            watchdog = SchedulerHeartbeatWatchdog(
                status_path=status_path,
                state_path=state_path,
                stale_after_minutes=15,
                startup_grace_seconds=0,
                now_func=lambda: current[0],
                monotonic_func=lambda: 100.0,
            )

            alert = watchdog.poll(("123",))
            self.assertIn("排程器異常", alert.message)
            watchdog.mark_delivered(alert, "123")
            duplicate = watchdog.poll(("123",))
            self.assertIsNone(duplicate)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["phase"],
                "incident",
            )

            current[0] += timedelta(minutes=1)
            status_path.write_text(
                json.dumps(
                    {
                        "state": "waiting",
                        "updated_at": current[0].isoformat(timespec="seconds"),
                    }
                ),
                encoding="utf-8",
            )
            recovery = watchdog.poll(("123",))
            self.assertIn("已恢復", recovery.message)
            watchdog.mark_delivered(recovery, "123")
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["phase"],
                "none",
            )

    def test_watchdog_startup_grace_avoids_resume_race(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "scheduler_status.json").write_text(
                json.dumps(
                    {
                        "state": "waiting",
                        "updated_at": "2026-08-12T12:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            monotonic = [100.0]
            watchdog = SchedulerHeartbeatWatchdog(
                status_path=root / "scheduler_status.json",
                state_path=root / "watchdog.json",
                stale_after_minutes=15,
                startup_grace_seconds=60,
                now_func=lambda: datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc),
                monotonic_func=lambda: monotonic[0],
            )
            self.assertIsNone(watchdog.poll(("123",)))
            monotonic[0] += 61
            self.assertIn("排程器異常", watchdog.poll(("123",)).message)


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.monitors = [
            {"id": 1, "origin": "TPE", "destination": "NRT", "quota_mode": "lean"},
            {"id": 2, "origin": "TPE", "destination": "KIX", "quota_mode": "standard"},
            {"id": 3, "origin": "TPE", "destination": "BKK", "quota_mode": "full"},
        ]
        self.database = FakeDatabase(self.monitors)

    def test_status_reads_scheduler_heartbeat_and_active_monitors(self):
        with tempfile.TemporaryDirectory() as temp:
            status_path = Path(temp) / "scheduler_status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "state": "waiting",
                        "updated_at": "2026-08-12T12:01:30+08:00",
                        "last_completed_at": "2026-08-12T12:01:00+08:00",
                        "next_run_at": "2026-08-13T00:00+08:00",
                    }
                ),
                encoding="utf-8",
            )
            callbacks = CommandCallbacks(
                database=self.database,
                status_path=status_path,
                now_func=lambda: datetime(2026, 8, 12, 4, 2, tzinfo=timezone.utc),
            )

            text = callbacks.status(None)

            self.assertIn("正常待命", text)
            self.assertIn("啟用監控：3 筆", text)
            self.assertIn("TPE→NRT（精簡）", text)
            self.assertIn("下次執行：2026-08-13", text)
            self.assertTrue(self.database.last_active_only)

    def test_status_never_reports_a_stale_heartbeat_as_healthy(self):
        with tempfile.TemporaryDirectory() as temp:
            status_path = Path(temp) / "scheduler_status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "state": "waiting",
                        "updated_at": "2026-08-12T12:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            callbacks = CommandCallbacks(
                database=self.database,
                status_path=status_path,
                stale_after_minutes=15,
                now_func=lambda: datetime(2026, 8, 12, 12, 16, tzinfo=timezone.utc),
            )
            text = callbacks.status(None)
        self.assertIn("心跳已中斷 16 分鐘", text)
        self.assertNotIn("正常待命", text)

    def test_quota_uses_database_summary_and_conservative_mode_total(self):
        callbacks = CommandCallbacks(database=self.database, monthly_limit=250)

        text = callbacks.quota(None)

        self.assertIn("12 / 250", text)
        self.assertIn("剩餘：238", text)
        self.assertIn("完整檢查上限 8 次", text)
        self.assertEqual(self.database.last_summary_args, ("serpapi", 250))

    def test_check_estimate_aggregates_every_active_monitor_mode(self):
        callbacks = CommandCallbacks(database=self.database)

        estimate = callbacks.estimate_check(None)

        self.assertEqual(estimate.api_calls, 8)
        self.assertIn("精簡 1 筆", estimate.detail)
        self.assertIn("標準 1 筆", estimate.detail)
        self.assertIn("完整 1 筆", estimate.detail)

    def test_check_confirmation_callback_runs_manual_check_and_summarises(self):
        check_func = Mock(
            return_value=[
                {
                    "monitor_id": 1,
                    "ok": True,
                    "best_price": 8880,
                    "hit": True,
                    "notified": True,
                    "api_calls": 1,
                },
                {
                    "monitor_id": 2,
                    "ok": False,
                    "hit": False,
                    "notified": False,
                    "api_calls": 2,
                },
            ]
        )
        callbacks = CommandCallbacks(database=self.database, check_func=check_func)

        text = callbacks.check(None)

        check_func.assert_called_once_with(scheduled_report=False)
        self.assertIn("完成 2 筆：成功 1、失敗 1", text)
        self.assertIn("實際 API 3 次", text)
        self.assertIn("TPE→NRT：NT$8,880（命中）", text)
        self.assertIn("TPE→KIX：查價失敗", text)

    def test_check_estimate_rejects_empty_monitor_set(self):
        callbacks = CommandCallbacks(database=FakeDatabase([]))

        with self.assertRaises(RuntimeError):
            callbacks.estimate_check(None)


class TelegramAPIAndLockTests(unittest.TestCase):
    def test_api_uses_injected_session_and_long_poll_timeout(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ok": True, "result": [telegram_update()]}
        session = Mock()
        session.post.return_value = response
        api = TelegramAPI("TEST_TOKEN", session=session, connect_timeout=4)

        result = api.get_updates(6, timeout=20)

        self.assertEqual(result[0]["update_id"], 10)
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs["json"]["offset"], 6)
        self.assertEqual(kwargs["json"]["allowed_updates"], ["message"])
        self.assertEqual(kwargs["timeout"], (4.0, 30))

    def test_single_instance_lock_blocks_live_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bot.lock"
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)

            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
