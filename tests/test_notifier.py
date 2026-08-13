import unittest
from unittest.mock import Mock, patch

from src.notifier import DUPLICATE_SUPPRESSED, _deliver_once, process_notification_outbox


class NotifierDedupeTests(unittest.TestCase):
    @patch("src.notifier.db.log_notification")
    @patch("src.notifier.db.claim_notification", return_value=False)
    def test_exact_duplicate_is_suppressed_before_network(self, _claim_mock, log_mock):
        sender = Mock(return_value=(True, "OK"))

        result = _deliver_once("telegram", 7, "same quote", sender)

        self.assertEqual(result, ("telegram", False, DUPLICATE_SUPPRESSED))
        sender.assert_not_called()
        log_mock.assert_not_called()

    @patch("src.notifier.db.log_notification")
    @patch("src.notifier.db.claim_notification", return_value=True)
    def test_claimed_delivery_is_sent_and_logged_once(self, _claim_mock, log_mock):
        sender = Mock(return_value=(True, "OK"))

        result = _deliver_once("telegram", 7, "fresh quote", sender)

        self.assertEqual(result, ("telegram", True, "OK"))
        sender.assert_called_once_with("fresh quote")
        log_mock.assert_called_once_with(7, "fresh quote", "telegram", True)

    @patch("src.notifier.db.log_notification")
    @patch("src.notifier.db.complete_outbox_delivery", return_value={"status": "retry", "attempt_count": 1, "next_attempt_at": "later"})
    @patch("src.notifier.db.claim_due_notifications")
    @patch("src.notifier.send_telegram", side_effect=RuntimeError("boom"))
    def test_unexpected_sender_error_returns_item_to_retry_state(
        self, _sender, claim_mock, complete_mock, _log_mock
    ):
        claim_mock.return_value = [
            {"id": 4, "channel": "telegram", "message": "hello", "monitor_id": 9}
        ]
        result = process_notification_outbox()
        self.assertFalse(result[0]["success"])
        self.assertEqual(result[0]["detail"], "sender_error:RuntimeError")
        complete_mock.assert_called_once_with(
            4, success=False, error="sender_error:RuntimeError"
        )


if __name__ == "__main__":
    unittest.main()
