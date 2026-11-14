import math
import unittest
from app.forecast_metrics import score_probabilities

class ForecastMetricsTests(unittest.TestCase):
    def test_perfect_prediction(self):
        self.assertEqual(score_probabilities({"home":1,"draw":0,"away":0},"home"),(0.0,0.0))

    def test_uniform_prediction(self):
        brier,loss=score_probabilities({"home":1/3,"draw":1/3,"away":1/3},"draw")
        self.assertAlmostEqual(brier,2/9)
        self.assertAlmostEqual(loss,math.log(3))

    def test_wrong_prediction_is_penalized(self):
        brier,loss=score_probabilities({"home":0.9,"draw":0.05,"away":0.05},"away")
        self.assertGreater(brier,0.4)
        self.assertGreater(loss,2.9)

    def test_invalid_values_rejected(self):
        for probs in ({"home":float("nan"),"draw":0.5,"away":0.5},
                      {"home":float("inf"),"draw":0,"away":0},
                      {"home":0.9,"draw":0.9,"away":0.9},
                      {"home":-0.1,"draw":0.5,"away":0.6},
                      {"home":0.5,"draw":0.5}):
            with self.assertRaises(ValueError):
                score_probabilities(probs,"home")

if __name__=="__main__":
    unittest.main()
