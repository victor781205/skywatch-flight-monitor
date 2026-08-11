import unittest
from unittest.mock import patch

from src.monitor import check_one_monitor, search_and_save


class MonitorTests(unittest.TestCase):
    @patch("src.monitor.send_all", return_value=[("Telegram", True, "ok")])
    @patch("src.monitor.db.claim_notification", return_value=True)
    @patch("src.monitor.db.recent_success_notification", return_value=None)
    @patch("src.monitor.db.add_price_record")
    @patch("src.monitor.db.update_monitor_check")
    @patch("src.monitor.get_label", side_effect=lambda code: code)
    @patch("src.monitor.search_flights")
    def test_unchanged_price_still_sends_scheduled_report(
        self,
        search_mock,
        _label_mock,
        _update_mock,
        _add_price_mock,
        _recent_mock,
        _claim_mock,
        send_mock,
    ):
        search_mock.return_value = {
            "best_price": 17378.0,
            "currency": "TWD",
            "flights": [
                {
                    "airline": "Test Air",
                    "stops": 0,
                    "duration": "3h",
                    "departure_time": "09:00",
                    "arrival_time": "12:00",
                    "flight_number": "TA1",
                }
            ],
        }
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": None,
            "target_price": 15510,
            "adults": 1,
            "travel_class": 1,
            "last_price": 17378.0,
        }

        with patch("src.monitor.ALWAYS_NOTIFY", True):
            result = check_one_monitor(monitor)

        self.assertTrue(result["notified"])
        self.assertEqual(result["notify_reason"], "sent")
        send_mock.assert_called_once()

    @patch("src.monitor.db.add_price_record")
    @patch("src.monitor.db.upsert_route", return_value=7)
    @patch("src.monitor.get_label", side_effect=lambda code: code)
    @patch("src.monitor.search_flights")
    def test_search_result_preserves_people_and_class(
        self, search_mock, _label_mock, _route_mock, add_price_mock
    ):
        search_mock.return_value = {
            "best_price": 12345.0,
            "currency": "TWD",
            "flights": [
                {
                    "airline": "Test Air",
                    "stops": 0,
                    "duration": "3h 0m",
                    "departure_time": "09:00",
                    "arrival_time": "12:00",
                    "flight_number": "TA1",
                }
            ],
        }
        result = search_and_save("TPE", "KIX", "2026-09-01", adults=2, travel_class=3)
        self.assertEqual(result["adults"], 2)
        self.assertEqual(result["travel_class"], 3)
        self.assertEqual(search_mock.call_args.kwargs["travel_class"], 3)
        self.assertEqual(add_price_mock.call_args.kwargs["travel_class"], 3)


if __name__ == "__main__":
    unittest.main()
