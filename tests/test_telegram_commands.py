import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from src.telegram_commands import (
    CheckEstimate,
    TelegramCommandRouter,
    parse_telegram_command,
)


class MutableClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class TelegramCommandTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.status = Mock(return_value="排程正常；下次執行 12:00。")
        self.quota = Mock(return_value="本月已使用 12 / 250 次。")
        self.estimate = Mock(return_value=CheckEstimate(3, "標準模式"))
        self.check = Mock(return_value="TPE → NRT 最低 NT$8,880。")
        self.tokens = iter(("TOKEN01", "TOKEN02", "TOKEN03", "TOKEN04"))
        self.router = TelegramCommandRouter(
            allowed_chat_ids={123, "-456"},
            status_callback=self.status,
            quota_callback=self.quota,
            check_estimate_callback=self.estimate,
            check_callback=self.check,
            bot_username="SkyWatchBot",
            confirmation_ttl_seconds=30,
            clock=self.clock,
            token_factory=lambda: next(self.tokens),
        )

    def test_parser_accepts_bot_username_suffix(self):
        parsed = parse_telegram_command(" /check@SkyWatchBot confirm AbC_123 ")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.name, "check")
        self.assertEqual(parsed.bot_suffix, "SkyWatchBot")
        self.assertEqual(parsed.arguments, ("confirm", "AbC_123"))

    def test_status_and_quota_call_injected_callbacks(self):
        status_result = self.router.handle("/status", 123, user_id=9)
        quota_result = self.router.handle("/quota@skywatchbot", "123", user_id=9)

        self.assertTrue(status_result.handled)
        self.assertIn("系統狀態", status_result.text)
        self.assertIn("下次執行 12:00", status_result.text)
        self.assertIn("API 額度", quota_result.text)
        self.assertIn("12 / 250", quota_result.text)
        self.status.assert_called_once()
        self.quota.assert_called_once()

    def test_command_for_another_bot_is_ignored(self):
        result = self.router.handle("/status@AnotherBot", 123)

        self.assertFalse(result.handled)
        self.status.assert_not_called()

    def test_unauthorised_chat_is_rejected_without_callback(self):
        result = self.router.handle("/status", 999)

        self.assertTrue(result.handled)
        self.assertFalse(result.ok)
        self.assertIn("未獲授權", result.text)
        self.status.assert_not_called()

    def test_string_allowlist_is_one_chat_id_not_character_ids(self):
        router = TelegramCommandRouter(
            allowed_chat_ids="123",
            status_callback=self.status,
            quota_callback=self.quota,
            check_estimate_callback=self.estimate,
            check_callback=self.check,
        )

        self.assertTrue(router.handle("/status", "123").ok)
        self.assertFalse(router.handle("/status", "1").ok)

    def test_check_only_estimates_and_returns_short_lived_token(self):
        result = self.router.handle("/check", 123, user_id=9)

        self.assertTrue(result.ok)
        self.assertFalse(result.executed)
        self.assertIn("預估會使用 3 次", result.text)
        self.assertIn("標準模式", result.text)
        self.assertIn("30 秒內", result.text)
        self.assertRegex(result.text, r"/check confirm TOKEN01")
        self.estimate.assert_called_once()
        self.check.assert_not_called()

    def test_confirm_executes_exactly_once_and_blocks_replay(self):
        self.router.handle("/check", 123, user_id=9, username="victor")

        first = self.router.handle(
            "/check confirm token01", 123, user_id=9, username="victor"
        )
        replay = self.router.handle("/check confirm TOKEN01", 123, user_id=9)

        self.assertTrue(first.ok)
        self.assertTrue(first.executed)
        self.assertIn("即時查價完成", first.text)
        self.assertFalse(replay.ok)
        self.assertFalse(replay.executed)
        self.assertIn("已使用", replay.text)
        self.check.assert_called_once()

    def test_parallel_confirmations_cannot_both_execute(self):
        self.router.handle("/check", 123, user_id=9)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self.router.handle, "/check confirm TOKEN01", 123, user_id=9
                )
                for _ in range(2)
            ]
        results = [future.result() for future in futures]

        self.assertEqual(sum(result.executed for result in results), 1)
        self.assertEqual(sum(result.ok for result in results), 1)
        self.check.assert_called_once()

    def test_token_is_bound_to_chat_and_user(self):
        self.router.handle("/check", 123, user_id=9)

        wrong_chat = self.router.handle("/check confirm TOKEN01", -456, user_id=9)
        wrong_user = self.router.handle("/check confirm TOKEN01", 123, user_id=10)
        owner = self.router.handle("/check confirm TOKEN01", 123, user_id=9)

        self.assertFalse(wrong_chat.ok)
        self.assertFalse(wrong_user.ok)
        self.assertTrue(owner.ok)
        self.check.assert_called_once()

    def test_expired_token_never_executes(self):
        self.router.handle("/check", 123, user_id=9)
        self.clock.value += 30

        result = self.router.handle("/check confirm TOKEN01", 123, user_id=9)

        self.assertFalse(result.ok)
        self.assertIn("逾時", result.text)
        self.check.assert_not_called()

    def test_new_token_replaces_previous_token_for_same_actor(self):
        first = self.router.handle("/check", 123, user_id=9)
        second = self.router.handle("/check", 123, user_id=9)

        self.assertIn("TOKEN01", first.text)
        self.assertIn("TOKEN02", second.text)
        old_result = self.router.handle("/check confirm TOKEN01", 123, user_id=9)
        new_result = self.router.handle("/check confirm TOKEN02", 123, user_id=9)
        self.assertIn("已失效", old_result.text)
        self.assertTrue(new_result.executed)
        self.check.assert_called_once()

    def test_unknown_and_invalid_commands_are_safe(self):
        unknown = self.router.handle("/delete_everything", 123)
        bad_status = self.router.handle("/status now", 123)
        malformed = self.router.handle("/???", 123)
        normal_chat = self.router.handle("你好", 123)

        self.assertIn("不支援", unknown.text)
        self.assertIn("用法：/status", bad_status.text)
        self.assertIn("格式不正確", malformed.text)
        self.assertFalse(normal_chat.handled)
        self.status.assert_not_called()

    def test_handle_update_extracts_chat_and_sender_context(self):
        result = self.router.handle_update(
            {
                "update_id": 1,
                "message": {
                    "text": "/status",
                    "chat": {"id": 123},
                    "from": {"id": 9, "username": "victor"},
                },
            }
        )

        self.assertTrue(result.ok)
        context = self.status.call_args.args[0]
        self.assertEqual(context.chat_id, "123")
        self.assertEqual(context.user_id, "9")
        self.assertEqual(context.username, "victor")

    def test_callback_errors_do_not_leak_exception_details(self):
        self.check.side_effect = RuntimeError("SECRET_API_KEY")
        self.router.handle("/check", 123, user_id=9)

        result = self.router.handle("/check confirm TOKEN01", 123, user_id=9)
        replay = self.router.handle("/check confirm TOKEN01", 123, user_id=9)

        self.assertFalse(result.ok)
        self.assertTrue(result.executed)
        self.assertNotIn("SECRET_API_KEY", result.text)
        self.assertIn("已使用", replay.text)
        self.check.assert_called_once()

    def test_invalid_estimate_does_not_create_confirmation(self):
        self.estimate.return_value = 0

        result = self.router.handle("/check", 123, user_id=9)

        self.assertFalse(result.ok)
        self.assertIn("尚未執行查價", result.text)
        self.check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
