import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.monitor import (
    _check_one_monitor_impl,
    _notification_claim_scope,
    check_one_monitor,
    search_and_save,
)


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

    def test_quota_guard_counts_failed_attempts_conservatively(self):
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": None,
            "target_price": 15000,
            "quota_mode": "full",
        }
        with (
            patch(
                "src.monitor.db.get_api_usage_summary",
                return_value={"used": 0, "attempted": 249},
            ),
            patch("src.monitor.SERPAPI_MONTHLY_LIMIT", 250),
            patch("src.monitor.search_flights") as search,
        ):
            result = _check_one_monitor_impl(monitor)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["error_stage"], "quota_guard")
        search.assert_not_called()

    def test_two_scheduled_runs_in_one_hour_have_distinct_notification_claims(self):
        now = datetime(2026, 8, 13, 12, 30, tzinfo=ZoneInfo("Asia/Taipei"))
        first = _notification_claim_scope(
            scheduled_report=True,
            run_id="slot-12-00",
            now=now,
        )
        second = _notification_claim_scope(
            scheduled_report=True,
            run_id="slot-12-30",
            now=now,
        )
        self.assertNotEqual(first, second)

    def test_manual_notification_claim_tracks_exact_payload_not_five_minute_bucket(self):
        now = datetime(2026, 8, 13, 12, 30, tzinfo=ZoneInfo("Asia/Taipei"))
        first = _notification_claim_scope(
            scheduled_report=False,
            run_id="manual-1",
            now=now,
            message="TPE → BKK：TWD 17,000",
        )
        same_payload = _notification_claim_scope(
            scheduled_report=False,
            run_id="manual-2",
            now=now + timedelta(minutes=1),
            message="TPE → BKK：TWD 17,000",
        )
        changed_payload = _notification_claim_scope(
            scheduled_report=False,
            run_id="manual-3",
            now=now + timedelta(minutes=1),
            message="TPE → BKK：TWD 16,500",
        )
        self.assertEqual(first, same_payload)
        self.assertNotEqual(first, changed_payload)

    def test_round_trip_carrier_rule_enrichment_triggers_target_only_notification(self):
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": "2026-09-08",
            "target_price": 15000,
            "last_price": 19000,
            "quota_mode": "full",
            "notification_policy": "target_only",
            "airline_rules_json": json.dumps(
                [
                    {
                        "airline": "EVA",
                        "target_price": 18000,
                        "direct_only": True,
                        "return_window": {"start": "08:00", "end": "11:00"},
                        "require_same_airline_roundtrip": True,
                    }
                ]
            ),
        }
        search_result = {
            "provider": "serpapi",
            "best_price": 17000,
            "currency": "TWD",
            "flights": [
                {
                    "price": 17000,
                    "airline": "EVA Air",
                    "flight_number": "BR101",
                    "departure_token": "eva-token",
                    "departure_time": "2026-09-01 09:00",
                    "arrival_time": "2026-09-01 12:00",
                    "stops": 0,
                }
            ],
        }
        return_flight = {
            "airline": "EVA Air",
            "flight_number": "BR102",
            "departure_time": "2026-09-08 09:00",
            "arrival_time": "2026-09-08 12:00",
            "stops": 0,
        }
        with (
            patch("src.monitor.search_flights", return_value=search_result),
            patch(
                "src.monitor.get_return_flight_info",
                return_value=return_flight,
            ) as return_details,
            patch("src.monitor.db.get_api_usage_summary", return_value={"attempted": 0}),
            patch("src.monitor.db.update_monitor_check"),
            patch("src.monitor.db.add_price_record"),
            patch("src.monitor.db.save_carrier_quotes"),
            patch("src.monitor.db.claim_notification", return_value=True),
            patch("src.monitor.get_label", side_effect=lambda code: code),
            patch(
                "src.monitor.send_all_reliable",
                return_value=[("telegram", True, "ok")],
            ) as send,
        ):
            result = _check_one_monitor_impl(monitor, run_id="carrier-run")

        self.assertTrue(result["hit"])
        self.assertTrue(result["notified"])
        self.assertEqual(result["quota_calls_used"], 2)
        self.assertTrue(
            any(match["target_hit"] for match in result["carrier_rule_matches"])
        )
        return_details.assert_called_once()
        self.assertIn("航空公司專屬目標已達標：EVA", send.call_args.args[0])

    def test_full_mode_hard_caps_five_calls_and_prioritises_all_carrier_returns(self):
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": "2026-09-08",
            "target_price": 20000,
            "last_price": 22000,
            "quota_mode": "full",
            "notification_policy": "always",
        }
        search_result = {
            "provider": "serpapi",
            "best_price": 17000,
            "currency": "TWD",
            "flights": [
                {
                    "price": 17000,
                    "airline": "Budget Air",
                    "flight_number": "LCC1",
                    "departure_token": "top-token",
                    "stops": 0,
                },
                {
                    "price": 18000,
                    "airline": "EVA Air",
                    "flight_number": "BR101",
                    "departure_token": "br-token",
                    "stops": 0,
                },
                {
                    "price": 18100,
                    "airline": "China Airlines",
                    "flight_number": "CI101",
                    "departure_token": "ci-token",
                    "stops": 0,
                },
                {
                    "price": 18200,
                    "airline": "STARLUX Airlines",
                    "flight_number": "JX101",
                    "departure_token": "jx-token",
                    "stops": 0,
                },
            ],
        }

        def return_for(token, *_args, **_kwargs):
            return {
                "airline": "Return Air",
                "flight_number": f"RET-{token}",
                "departure_time": "09:00",
                "arrival_time": "12:00",
                "booking_token": "booking-token" if token == "top-token" else "",
            }

        with (
            patch("src.monitor.search_flights", return_value=search_result) as search,
            patch("src.monitor.get_return_flight_info", side_effect=return_for) as returns,
            patch("src.monitor.get_booking_provider") as booking,
            patch("src.monitor.db.get_api_usage_summary", return_value={"attempted": 0}),
            patch("src.monitor.db.update_monitor_check"),
            patch("src.monitor.db.add_price_record"),
            patch("src.monitor.db.save_carrier_quotes"),
            patch("src.monitor.db.claim_notification", return_value=True),
            patch("src.monitor.get_label", side_effect=lambda code: code),
            patch("src.monitor.send_all", return_value=[("telegram", True, "ok")]),
            patch("src.monitor.MONITOR_FETCH_DETAILS_ON_HIT", True),
        ):
            result = _check_one_monitor_impl(monitor, scheduled_report=True)

        self.assertEqual(search.call_count + returns.call_count + booking.call_count, 5)
        self.assertEqual(result["quota_calls_used"], 5)
        self.assertEqual(
            {call.args[0] for call in returns.call_args_list},
            {"top-token", "br-token", "ci-token", "jx-token"},
        )
        booking.assert_not_called()

    def test_successful_backfill_gets_distinct_ledger_status(self):
        monitor = {"id": 5, "next_run_at": "2026-08-13T12:00:00+08:00"}
        with (
            patch(
                "src.monitor.db.create_check_run",
                return_value={"id": "run-5", "created": True},
            ),
            patch(
                "src.monitor.db.get_api_usage_summary",
                side_effect=[{"attempted": 10}, {"attempted": 11}],
            ),
            patch(
                "src.monitor._check_one_monitor_impl",
                return_value={"ok": True, "monitor_id": 5},
            ),
            patch("src.monitor.db.finish_check_run") as finish,
            patch("src.monitor.db.set_monitor_next_run"),
        ):
            check_one_monitor(
                monitor,
                scheduled_report=True,
                scheduled_slot="2026-08-12T12:00+08:00",
                backfill=True,
            )
        self.assertEqual(finish.call_args.kwargs["status"], "backfilled")

    def test_quota_guard_run_is_recorded_as_skipped(self):
        monitor = {"id": 5, "next_run_at": "2026-08-13T12:00:00+08:00"}
        with (
            patch(
                "src.monitor.db.create_check_run",
                return_value={"id": "run-guard", "created": True},
            ),
            patch(
                "src.monitor.db.get_api_usage_summary",
                side_effect=[{"attempted": 249}, {"attempted": 249}],
            ),
            patch(
                "src.monitor._check_one_monitor_impl",
                return_value={
                    "ok": False,
                    "skipped": True,
                    "error_stage": "quota_guard",
                    "error": "quota exhausted",
                    "notify_results": [],
                },
            ),
            patch("src.monitor.db.finish_check_run") as finish,
            patch("src.monitor.db.set_monitor_next_run"),
        ):
            check_one_monitor(monitor, scheduled_report=True)
        self.assertEqual(finish.call_args.kwargs["status"], "skipped")
        self.assertEqual(finish.call_args.kwargs["error_stage"], "quota_guard")

    def test_carrier_quotes_retain_br_ci_jx_beyond_first_five(self):
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": None,
            "target_price": 5000,
            "quota_mode": "full",
            "notification_policy": "target_only",
        }
        flights = [
            {
                "price": 6000 + index * 100,
                "airline": f"Budget {index}",
                "flight_number": f"LCC{index}",
                "stops": 0,
            }
            for index in range(5)
        ] + [
            {"price": 9000, "airline": "EVA Air", "flight_number": "BR101", "stops": 0},
            {"price": 9100, "airline": "China Airlines", "flight_number": "CI101", "stops": 0},
            {"price": 9200, "airline": "STARLUX Airlines", "flight_number": "JX101", "stops": 0},
        ]
        search_result = {
            "provider": "serpapi",
            "best_price": 6000,
            "currency": "TWD",
            "flights": flights,
        }
        with (
            patch("src.monitor.search_flights", return_value=search_result),
            patch("src.monitor.db.get_api_usage_summary", return_value={"attempted": 0}),
            patch("src.monitor.db.update_monitor_check"),
            patch("src.monitor.db.add_price_record"),
            patch("src.monitor.db.save_carrier_quotes") as save,
        ):
            _check_one_monitor_impl(monitor, run_id="run-1")
        saved_codes = {quote["carrier_code"] for quote in save.call_args.kwargs["quotes"]}
        self.assertTrue({"BR", "CI", "JX"}.issubset(saved_codes))

    def test_quiet_hours_persist_delivery_without_sending(self):
        current = datetime.now(ZoneInfo("Asia/Taipei"))
        quiet = {
            "start": (current - timedelta(hours=1)).strftime("%H:%M"),
            "end": (current + timedelta(hours=1)).strftime("%H:%M"),
        }
        monitor = {
            "id": 1,
            "route_id": 7,
            "origin": "TPE",
            "destination": "BKK",
            "outbound_date": "2026-09-01",
            "return_date": None,
            "target_price": 10000,
            "quota_mode": "lean",
            "notification_policy": "target_only",
            "quiet_hours_json": json.dumps(quiet),
        }
        search_result = {
            "provider": "serpapi",
            "best_price": 9000,
            "currency": "TWD",
            "flights": [
                {
                    "price": 9000,
                    "airline": "EVA Air",
                    "flight_number": "BR101",
                    "departure_time": "09:00",
                    "arrival_time": "12:00",
                    "stops": 0,
                }
            ],
        }
        with (
            patch("src.monitor.search_flights", return_value=search_result),
            patch("src.monitor.db.get_api_usage_summary", return_value={"attempted": 0}),
            patch("src.monitor.db.update_monitor_check"),
            patch("src.monitor.db.add_price_record"),
            patch("src.monitor.db.save_carrier_quotes"),
            patch("src.monitor.get_label", side_effect=lambda code: code),
            patch(
                "src.monitor.enqueue_all",
                return_value=[{"id": 3, "channel": "telegram", "status": "pending"}],
            ) as enqueue,
            patch("src.monitor.send_all") as send,
            patch("src.monitor.send_all_reliable") as reliable_send,
        ):
            result = _check_one_monitor_impl(monitor, run_id="run-quiet")
        self.assertEqual(result["notify_reason"], "quiet_hours_queued")
        self.assertFalse(result["notified"])
        self.assertTrue(enqueue.call_args.kwargs["replace_pending"])
        self.assertGreater(enqueue.call_args.kwargs["next_attempt_at"], datetime.now())
        send.assert_not_called()
        reliable_send.assert_not_called()

    def test_intentional_quiet_queue_keeps_run_ledger_successful(self):
        monitor = {"id": 5, "next_run_at": "2026-08-14T00:00:00+08:00"}
        with (
            patch(
                "src.monitor.db.create_check_run",
                return_value={"id": "run-quiet-ledger", "created": True},
            ),
            patch(
                "src.monitor.db.get_api_usage_summary",
                side_effect=[{"attempted": 10}, {"attempted": 11}],
            ),
            patch(
                "src.monitor._check_one_monitor_impl",
                return_value={
                    "ok": True,
                    "notify_reason": "quiet_hours_queued",
                    "notify_results": [
                        ("telegram", False, "queued_for_quiet_end")
                    ],
                },
            ),
            patch("src.monitor.db.finish_check_run") as finish,
            patch("src.monitor.db.set_monitor_next_run"),
        ):
            check_one_monitor(monitor, scheduled_report=True)
        self.assertEqual(finish.call_args.kwargs["status"], "succeeded")
        self.assertEqual(finish.call_args.kwargs["error_stage"], "")


if __name__ == "__main__":
    unittest.main()
