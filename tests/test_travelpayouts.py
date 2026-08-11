import unittest
from unittest.mock import Mock, patch

from src.travelpayouts import get_market_reference


class TravelpayoutsTests(unittest.TestCase):
    @patch("src.travelpayouts.requests.get")
    def test_exact_date_observation_has_priority(self, get_mock):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "success": True,
            "data": {
                "2026-09-01": {"price": 12000, "airline": "BR", "transfers": 0},
                "2026-09-03": {"price": 9000, "airline": "CI", "transfers": 1},
            },
        }
        get_mock.return_value = response
        with patch("src.travelpayouts.TRAVELPAYOUTS_TOKEN", "secret-token"):
            result = get_market_reference("tpe", "kix", "2026-09-01")
        self.assertEqual(result["match"], "exact_date")
        self.assertEqual(result["price"], 12000)
        self.assertTrue(result["cached"])
        self.assertEqual(get_mock.call_args.kwargs["headers"]["X-Access-Token"], "secret-token")

    @patch("src.travelpayouts.requests.get")
    def test_month_low_is_used_without_exact_date(self, get_mock):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "success": True,
            "data": {
                "2026-09-02": {"price": 11000},
                "2026-09-03": {"price": 9000},
                "2026-10-01": {"price": 1000},
            },
        }
        get_mock.return_value = response
        with patch("src.travelpayouts.TRAVELPAYOUTS_TOKEN", "secret-token"):
            result = get_market_reference("TPE", "KIX", "2026-09-01")
        self.assertEqual(result["match"], "monthly_low")
        self.assertEqual(result["departure_date"], "2026-09-03")
        self.assertEqual(result["price"], 9000)


if __name__ == "__main__":
    unittest.main()
