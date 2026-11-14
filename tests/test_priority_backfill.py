"""Priority league backfill tests without external API or database."""
import unittest
from unittest.mock import patch
from app.backfill_priority import backfill_priority

class FakeClient:
    calls = []
    def __init__(self):
        pass
    def competition_fixtures_range(self, code, start, end):
        self.calls.append((code,start,end))
        return [{"fixture":{"status":{"short":"FT"},"date":start+"T12:00:00Z"},"goals":{"home":1,"away":0}},
                {"fixture":{"status":{"short":"NS"},"date":start+"T14:00:00Z"},"goals":{"home":None,"away":None}}]
    def close(self):
        pass

class PriorityBackfillTests(unittest.TestCase):
    def test_rejects_invalid_inputs(self):
        for codes,h,f,w in [(("INVALID",),90,14,7),(("BSA",),91,14,7),(("BSA",),90,15,7),(("BSA",),90,14,0)]:
            with self.assertRaises(ValueError):
                backfill_priority(codes,h,f,w)

    def test_targeted_fetch_and_status_filter(self):
        FakeClient.calls=[]
        batches=[]
        with patch("app.backfill_priority.FootballDataClient",FakeClient),patch(
            "app.backfill_priority.store_fixtures",side_effect=lambda x:batches.append(x) or len(x)
        ):
            result=backfill_priority(("BSA",),history_days=8,future_days=0,window_days=4)
        self.assertEqual(len(FakeClient.calls),3)
        self.assertTrue(all(x[0]=="BSA" for x in FakeClient.calls))
        self.assertEqual(result["BSA"]["historical_processed"],2)
        self.assertEqual(result["BSA"]["current_and_future_processed"],2)
        self.assertEqual([len(x) for x in batches],[1,1,2])

if __name__=="__main__":
    unittest.main()
