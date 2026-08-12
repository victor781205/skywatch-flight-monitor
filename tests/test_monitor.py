import unittest
from unittest.mock import patch

from src.monitor import check_one_monitor, search_and_save


class MonitorTests(unittest.TestCase):
    @staticmethod
    def _round_trip_result():
        return {
            "provider": "serpapi",
            "best_price": 17378.0,
            "currency": "TWD",
            "flights": [
                {
                    "price": 17378.0,
                    "airline": "Test Air",
                    "stops": 0,
                    "duration": "3h",
                    "departure_time": "2026-09-01 09:00",
                    "arrival_time": "2026-09-01 12:00",
                    "flight_number": "TA1",
                    "departure_token": "departure-token",
                }
            ],
        }

    @patch("src.monitor.send_all", return_value=[("Telegram", True, "ok")])
    @patch("src.monitor.db.claim_notification", return_value=True)
    @patch("src.monitor.db.recent_success_notification", return_value={"id": 99})
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
            result = check_one_monitor(monitor, scheduled_report=True)

        self.assertTrue(result["notified"])
        self.assertEqual(result["notify_reason"], "sent")
        send_mock.assert_called_once()
        _recent_mock.assert_not_called()
        self.assertIn("排程回報：", send_mock.call_args.args[0])

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

    def test_non_hit_manual_report_does_not_fetch_details(self):
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": "2026-09-08",
            "target_price": 15000,
            "adults": 1,
            "travel_class": 1,
            "last_price": 17378.0,
        }
        with (
            patch("src.monitor.search_flights", return_value=self._round_trip_result()),
            patch("src.monitor.get_return_flight_info") as return_details,
            patch("src.monitor.get_booking_provider") as booking_details,
            patch("src.monitor.get_label", side_effect=lambda code: code),
            patch("src.monitor.db.update_monitor_check"),
            patch("src.monitor.db.add_price_record"),
            patch("src.monitor.db.recent_success_notification", return_value=None),
            patch("src.monitor.db.claim_notification", return_value=True),
            patch("src.monitor.send_all", return_value=[("Telegram", True, "ok")]),
            patch("src.monitor.ALWAYS_NOTIFY", True),
        ):
            result = check_one_monitor(monitor)
        self.assertFalse(result["hit"])
        return_details.assert_not_called()
        booking_details.assert_not_called()

    def test_non_hit_fixed_schedule_fetches_return_but_not_booking(self):
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": "2026-09-08",
            "target_price": 15000,
            "adults": 1,
            "travel_class": 1,
            "last_price": 17378.0,
        }
        return_value = {
            "airline": "Test Air",
            "flight_number": "TA2",
            "departure_time": "2026-09-08 09:00",
            "arrival_time": "2026-09-08 12:00",
            "booking_token": "booking-token",
        }
        with (
            patch("src.monitor.search_flights", return_value=self._round_trip_result()),
            patch("src.monitor.get_return_flight_info", return_value=return_value) as return_details,
            patch("src.monitor.get_booking_provider") as booking_details,
            patch("src.monitor.get_label", side_effect=lambda code: code),
            patch("src.monitor.db.update_monitor_check"),
            patch("src.monitor.db.add_price_record"),
            patch("src.monitor.db.claim_notification", return_value=True),
            patch("src.monitor.send_all", return_value=[("Telegram", True, "ok")]) as send_mock,
            patch("src.monitor.ALWAYS_NOTIFY", True),
            patch("src.monitor.MONITOR_FETCH_DETAILS_ON_HIT", True),
        ):
            result = check_one_monitor(monitor, scheduled_report=True)
        self.assertFalse(result["hit"])
        return_details.assert_called_once()
        booking_details.assert_not_called()
        self.assertIn("回程：Test Air TA2", send_mock.call_args.args[0])

    def test_fixed_schedule_fetches_each_premium_return_once(self):
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": "2026-09-08",
            "target_price": 15000,
            "adults": 1,
            "travel_class": 1,
            "last_price": 20000.0,
        }
        search_result = self._round_trip_result()
        search_result["best_price"] = 17000.0
        search_result["flights"] = [
            {
                "price": 17000.0,
                "airline": airline,
                "stops": 0,
                "duration": "3h",
                "departure_time": "2026-09-01 09:00",
                "arrival_time": "2026-09-01 12:00",
                "flight_number": outbound_number,
                "departure_token": token,
            }
            for airline, outbound_number, token in (
                ("EVA Air", "BR101", "eva-token"),
                ("China Airlines", "CI101", "china-token"),
                ("STARLUX Airlines", "JX101", "starlux-token"),
            )
        ]
        return_numbers = {
            "eva-token": ("EVA Air", "BR102"),
            "china-token": ("China Airlines", "CI102"),
            "starlux-token": ("STARLUX Airlines", "JX102"),
        }

        def return_for(token, *_args, **_kwargs):
            airline, number = return_numbers[token]
            return {
                "airline": airline,
                "flight_number": number,
                "departure_time": "2026-09-08 09:00",
                "arrival_time": "2026-09-08 12:00",
            }

        with (
            patch("src.monitor.search_flights", return_value=search_result),
            patch("src.monitor.get_return_flight_info", side_effect=return_for) as return_details,
            patch("src.monitor.get_booking_provider") as booking_details,
            patch("src.monitor.get_label", side_effect=lambda code: code),
            patch("src.monitor.db.update_monitor_check"),
            patch("src.monitor.db.add_price_record"),
            patch("src.monitor.db.claim_notification", return_value=True),
            patch("src.monitor.send_all", return_value=[("Telegram", True, "ok")]) as send_mock,
            patch("src.monitor.ALWAYS_NOTIFY", True),
            patch("src.monitor.MONITOR_FETCH_DETAILS_ON_HIT", True),
        ):
            result = check_one_monitor(monitor, scheduled_report=True)

        self.assertFalse(result["hit"])
        self.assertEqual(return_details.call_count, 3)
        booking_details.assert_not_called()
        message = send_mock.call_args.args[0]
        self.assertIn("來回總價：TWD 17,000", message)
        self.assertIn("回程：BR102", message)
        self.assertIn("回程：CI102", message)
        self.assertIn("回程：JX102", message)

    def test_hit_fetches_only_top_flight_details(self):
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": "2026-09-08",
            "target_price": 18000,
            "adults": 1,
            "travel_class": 1,
            "last_price": 19000.0,
        }
        return_value = {
            "airline": "Test Air",
            "flight_number": "TA2",
            "departure_time": "2026-09-08 09:00",
            "arrival_time": "2026-09-08 12:00",
            "booking_token": "booking-token",
        }
        with (
            patch("src.monitor.search_flights", return_value=self._round_trip_result()),
            patch("src.monitor.get_return_flight_info", return_value=return_value) as return_details,
            patch("src.monitor.get_booking_provider", return_value="Airline site") as booking_details,
            patch("src.monitor.get_label", side_effect=lambda code: code),
            patch("src.monitor.db.update_monitor_check"),
            patch("src.monitor.db.add_price_record"),
            patch("src.monitor.db.recent_success_notification", return_value=None),
            patch("src.monitor.db.claim_notification", return_value=True),
            patch("src.monitor.send_all", return_value=[("Telegram", True, "ok")]),
            patch("src.monitor.ALWAYS_NOTIFY", True),
            patch("src.monitor.MONITOR_FETCH_DETAILS_ON_HIT", True),
        ):
            result = check_one_monitor(monitor)
        self.assertTrue(result["hit"])
        return_details.assert_called_once()
        booking_details.assert_called_once()


if __name__ == "__main__":
    unittest.main()
