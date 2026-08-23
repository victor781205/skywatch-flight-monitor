import unittest
from unittest.mock import Mock, patch

import requests

from src.notifier import (
    DELIVERY_UNKNOWN,
    DUPLICATE_SUPPRESSED,
    _deliver_once,
    process_notification_outbox,
    send_telegram,
)


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
    @patch("src.notifier.db.outbox_delivery_is_owned", return_value=True)
    @patch("src.notifier.db.claim_due_notifications")
    @patch("src.notifier.send_telegram", side_effect=RuntimeError("boom"))
    def test_unexpected_sender_error_returns_item_to_retry_state(
        self, _sender, claim_mock, _owned, complete_mock, _log_mock
    ):
        claim_mock.return_value = [
            {"id": 4, "channel": "telegram", "message": "hello", "monitor_id": 9}
        ]
        result = process_notification_outbox()
        self.assertFalse(result[0]["success"])
        self.assertEqual(result[0]["detail"], "sender_error:RuntimeError")
        complete_mock.assert_called_once_with(
            4,
            success=False,
            error="sender_error:RuntimeError",
            lease_token="",
            delivery_unknown=False,
            retry_after_seconds=None,
            provider_message_id=None,
            provider_receipt=None,
        )

    @patch("src.notifier.requests.post", side_effect=requests.Timeout())
    def test_telegram_timeout_is_delivery_unknown(self, _post):
        ok, detail = send_telegram("ambiguous")
        self.assertFalse(ok)
        self.assertEqual(detail["detail"], DELIVERY_UNKNOWN)
        self.assertTrue(detail["delivery_unknown"])

    @patch("src.notifier.db.log_notification")
    @patch(
        "src.notifier.db.complete_outbox_delivery",
        return_value={
            "status": "sent",
            "attempt_count": 1,
            "next_attempt_at": "now",
        },
    )
    @patch("src.notifier.db.outbox_delivery_is_owned", return_value=True)
    @patch("src.notifier.db.claim_due_notifications")
    @patch("src.notifier.send_telegram")
    def test_outbox_persists_telegram_message_receipt(
        self, sender, claim, _owned, complete, _log
    ):
        claim.return_value = [
            {
                "id": 8,
                "channel": "telegram",
                "message": "hello",
                "monitor_id": 2,
                "lease_token": "fence-8",
            }
        ]
        sender.return_value = (
            True,
            {
                "detail": "OK",
                "message_id": 91,
                "receipt": {"message_id": 91, "chat_id": 123},
            },
        )
        process_notification_outbox()
        complete.assert_called_once_with(
            8,
            success=True,
            error="",
            lease_token="fence-8",
            delivery_unknown=False,
            retry_after_seconds=None,
            provider_message_id=91,
            provider_receipt={"message_id": 91, "chat_id": 123},
        )

    @patch("src.notifier.db.log_notification")
    @patch("src.notifier.db.complete_outbox_delivery")
    @patch("src.notifier.db.outbox_delivery_is_owned", return_value=False)
    @patch("src.notifier.db.claim_due_notifications")
    @patch("src.notifier.send_telegram")
    def test_revoked_outbox_lease_is_not_sent(
        self, sender, claim, _owned, complete, log
    ):
        claim.return_value = [
            {
                "id": 12,
                "channel": "telegram",
                "message": "obsolete fare",
                "monitor_id": 2,
                "lease_token": "revoked",
            }
        ]

        self.assertEqual(process_notification_outbox(), [])
        sender.assert_not_called()
        complete.assert_not_called()
        log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
