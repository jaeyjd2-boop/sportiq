"""Historical backfill tests; no network or production database access."""
import unittest
from datetime import date
from unittest.mock import patch
from app.collector import collect_historical

class FakeClient:
    calls = []
    def __init__(self):
        self.calls = FakeClient.calls
    def fixtures_range(self, start, end):
        self.calls.append((start,end))
        return [
            {"fixture":{"status":{"short":"FT"}},"goals":{"home":2,"away":1}},
            {"fixture":{"status":{"short":"NS"}},"goals":{"home":None,"away":None}},
            {"fixture":{"status":{"short":"FT"}},"goals":{"home":None,"away":1}},
        ]
    def close(self):
        pass

class BackfillTests(unittest.TestCase):
    def test_invalid_ranges_rejected_before_network(self):
        for days,window in [(0,7),(91,7),(30,0),(30,15)]:
            with self.assertRaises(ValueError):
                collect_historical(days,window)

    def test_completed_scored_only_and_windowing(self):
        FakeClient.calls=[]
        stored=[]
        with patch("app.collector.FootballDataClient", FakeClient), patch(
            "app.collector.store_fixtures", side_effect=lambda rows: stored.append(rows) or len(rows)
        ):
            result=collect_historical(days_back=15,window_days=7)
        self.assertEqual(result,3)
        self.assertEqual(len(FakeClient.calls),3)
        self.assertTrue(all(len(batch)==1 for batch in stored))
        self.assertTrue(all((date.fromisoformat(b)-date.fromisoformat(a)).days<=6 for a,b in FakeClient.calls))
        for (_,end),(next_start,_) in zip(FakeClient.calls,FakeClient.calls[1:]):
            self.assertEqual((date.fromisoformat(next_start)-date.fromisoformat(end)).days,1)

if __name__ == "__main__":
    unittest.main()
