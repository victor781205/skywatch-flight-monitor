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
    def test_selected_travel_class_reaches_serpapi(self, get_mock):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"best_flights": []}
        get_mock.return_value = response
        with patch("src.flight_api.SERPAPI_API_KEY", "test-key"):
            result = search_flights("TPE", "KIX", "2026-09-01", travel_class=3)
        self.assertIsNone(result["best_price"])
        self.assertEqual(get_mock.call_args.kwargs["params"]["travel_class"], 3)

    def test_amadeus_is_used_when_serpapi_fails(self):
        fallback_result = {"provider": "amadeus", "best_price": 9999.0, "flights": []}
        with (
            patch("src.flight_api.FLIGHT_API_PROVIDER", "serpapi"),
            patch("src.flight_api.FLIGHT_API_FALLBACK", "amadeus"),
            patch("src.flight_api.SERPAPI_API_KEY", "test-key"),
            patch("src.flight_api._search_serpapi", side_effect=FlightAPIError("quota")),
            patch("src.flight_api.amadeus.configured", return_value=True),
            patch("src.flight_api.amadeus.search_flights", return_value=fallback_result) as amadeus_search,
            patch("src.flight_api._record_usage"),
        ):
            result = search_flights("TPE", "KIX", "2026-09-01")
        self.assertEqual(result["provider"], "amadeus")
        amadeus_search.assert_called_once()


if __name__ == "__main__":
    unittest.main()
