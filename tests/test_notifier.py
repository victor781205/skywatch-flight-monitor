import unittest
from unittest.mock import Mock, patch

from src.notifier import DUPLICATE_SUPPRESSED, _deliver_once


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


if __name__ == "__main__":
    unittest.main()
