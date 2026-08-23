import unittest
from unittest.mock import Mock, patch

from src.flight_api import FlightAPIError, search_flights


class FlightApiTests(unittest.TestCase):
    def test_rejects_same_airport(self):
        with patch("src.flight_api.SERPAPI_API_KEY", "test-key"):
            with self.assertRaises(FlightAPIError):
                search_flights("TPE", "TPE", "2026-09-01")

    def test_rejects_return_before_departure(self):
        with patch("src.flight_api.SERPAPI_API_KEY", "test-key"):
            with self.assertRaises(FlightAPIError):
                search_flights("TPE", "KIX", "2026-09-10", "2026-09-01")

    @patch("src.flight_api.requests.get")
    @patch("src.flight_api.db.reconcile_api_quota_request")
    @patch(
        "src.flight_api.db.reserve_api_quota_request",
        return_value={"allowed": True, "token": "quota-one"},
    )
    def test_selected_travel_class_reaches_serpapi(
        self, _reserve, reconcile, get_mock
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"best_flights": []}
        get_mock.return_value = response
        with patch("src.flight_api.SERPAPI_API_KEY", "test-key"):
            result = search_flights("TPE", "KIX", "2026-09-01", travel_class=3)
        self.assertIsNone(result["best_price"])
        self.assertEqual(get_mock.call_args.kwargs["params"]["travel_class"], 3)
        reconcile.assert_called_once_with(
            "quota-one", attempted=True, success=True, detail="ok"
        )

    @patch("src.flight_api.requests.get")
    @patch(
        "src.flight_api.db.reserve_api_quota_request",
        side_effect=RuntimeError("database locked"),
    )
    def test_quota_ledger_failure_blocks_network(self, _reserve, get_mock):
        with patch("src.flight_api.SERPAPI_API_KEY", "test-key"):
            with self.assertRaises(FlightAPIError):
                search_flights("TPE", "KIX", "2026-09-01")
        get_mock.assert_not_called()

    @patch("src.flight_api.db.reconcile_api_quota_request")
    @patch(
        "src.flight_api.db.reserve_api_quota_request",
        return_value={"allowed": True, "token": "quota-429"},
    )
    @patch("src.flight_api.requests.get")
    def test_http_429_exposes_retry_after(self, get_mock, _reserve, reconcile):
        response = Mock(status_code=429, headers={"Retry-After": "17"})
        response.json.return_value = {"error": "rate limit"}
        get_mock.return_value = response
        with patch("src.flight_api.SERPAPI_API_KEY", "test-key"):
            with self.assertRaises(FlightAPIError) as raised:
                search_flights("TPE", "KIX", "2026-09-01")
        self.assertEqual(raised.exception.retry_after, 17.0)
        reconcile.assert_called_once_with(
            "quota-429", attempted=True, success=False, detail="http_429"
        )

if __name__ == "__main__":
    unittest.main()
