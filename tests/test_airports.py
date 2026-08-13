import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from src import airports


class AirportFallbackTests(unittest.TestCase):
    def tearDown(self):
        airports.load_airports.cache_clear()
        airports.airport_label_map.cache_clear()
        airports._AIRPORT_DATA_SOURCE = "unknown"

    def test_network_failure_uses_popular_airports_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(airports, "DATA_DIR", root),
                patch.object(airports, "AIRPORTS_CSV", root / "airports_iata.csv"),
                patch.object(airports, "AIRPORTS_JSON", root / "airports_iata.json"),
                patch.object(
                    airports.requests,
                    "get",
                    side_effect=requests.ConnectionError("offline"),
                ),
            ):
                airports.load_airports.cache_clear()
                airports.airport_label_map.cache_clear()
                options = airports.options_for_select("台北")
                self.assertIn("TPE", options.values())
                self.assertEqual(airports.airport_data_status(), "fallback")
                self.assertIn("TPE", airports.get_label("TPE"))


if __name__ == "__main__":
    unittest.main()
