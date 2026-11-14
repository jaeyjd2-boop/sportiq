"""Regression checks for the low-quota result refresh and result status mapping."""
import unittest
from unittest.mock import patch
from app.providers.football_data import FootballDataClient


class FootballDataNormalizationTests(unittest.TestCase):
    def test_finished_status_and_fulltime_score(self):
        client = object.__new__(FootballDataClient)
        item = {
            "id": 123, "utcDate": "2026-11-15T00:00:00Z",
            "status": "FINISHED",
            "competition": {"id": 2013, "name": "Serie A"},
            "homeTeam": {"id": 1, "name": "Home"},
            "awayTeam": {"id": 2, "name": "Away"},
            "score": {"fullTime": {"home": 2, "away": 1}},
        }
        result = client._normalize(item)
        self.assertEqual(result["fixture"]["status"]["short"], "FT")
        self.assertEqual(result["goals"], {"home": 2, "away": 1})
        self.assertEqual(result["fixture"]["id"], "fd:123")

    def test_scheduled_status(self):
        client = object.__new__(FootballDataClient)
        result = client._normalize({
            "id": 123, "status": "SCHEDULED",
            "homeTeam": {"id": 1}, "awayTeam": {"id": 2},
            "competition": {"id": 2013},
        })
        self.assertEqual(result["fixture"]["status"]["short"], "NS")


class ForecastResultRefreshTests(unittest.TestCase):
    def test_refresh_is_scoped_to_one_competition(self):
        from app.collector import refresh_forecast_results
        with patch("app.collector.FootballDataClient") as client, patch("app.collector.store_fixtures", return_value=2) as store:
            client.return_value.competition_fixtures_range.return_value = [{"fixture": {"id": "fd:123"}}]
            self.assertEqual(refresh_forecast_results(), 2)
            args = client.return_value.competition_fixtures_range.call_args.args
            self.assertEqual(args[0], "BSA")
            store.assert_called_once()
            client.return_value.close.assert_called_once()

    def test_invalid_window_rejected(self):
        from app.collector import refresh_forecast_results
        for value in (0, 4, -1, "2"):
            with self.assertRaises(ValueError):
                refresh_forecast_results(value)


if __name__ == "__main__":
    unittest.main()
