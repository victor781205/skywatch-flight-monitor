import unittest
from datetime import datetime
from unittest.mock import patch

from src.api_usage import estimate_monthly_primary_searches, scheduled_runs_per_day


class ApiUsageTests(unittest.TestCase):
    def test_fixed_schedule_projection(self):
        with patch.dict("os.environ", {"SCHEDULE_HOURS": "0,12", "CHECK_INTERVAL_MINUTES": ""}):
            self.assertEqual(scheduled_runs_per_day(), 2.0)
            self.assertEqual(
                estimate_monthly_primary_searches(4, now=datetime(2026, 8, 11)),
                248,
            )

    def test_interval_schedule_projection(self):
        with patch.dict("os.environ", {"CHECK_INTERVAL_MINUTES": "720"}):
            self.assertEqual(scheduled_runs_per_day(), 2.0)


if __name__ == "__main__":
    unittest.main()
