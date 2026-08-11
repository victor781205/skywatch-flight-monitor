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


if __name__ == "__main__":
    unittest.main()
