"""Elo forecasts for basketball, hockey and American football.

Scratch SQLite only: no network, no production database.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app import details, team_elo

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def make_engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE sports(id INTEGER PRIMARY KEY, code TEXT)"))
        c.execute(text("""CREATE TABLE matches(id INTEGER PRIMARY KEY, sport_id INT, home_team_id INT,
            away_team_id INT, start_time TIMESTAMP, status TEXT, home_score REAL, away_score REAL)"""))
        c.execute(text("INSERT INTO sports(id,code) VALUES (1,'football'),(2,'basketball'),(3,'hockey'),"
                       "(4,'american_football'),(5,'tennis')"))
    return eng


def add(eng, mid, sport, home, away, when, status="FT", hs=None, as_=None):
    with eng.begin() as c:
        c.execute(text("INSERT INTO matches VALUES (:id,:s,:h,:a,:t,:st,:hs,:as)"),
                  {"id": mid, "s": sport, "h": home, "a": away, "t": when, "st": status, "hs": hs, "as": as_})


def season(eng, sport, n_rounds, start_id=1, strong=10, weak=20, others=(30, 40)):
    """Strong team beats everyone, weak team loses to everyone."""
    mid, when = start_id, T0
    for _ in range(n_rounds):
        for home, away, hs, as_ in ((strong, weak, 110, 90), (others[0], strong, 95, 105),
                                    (weak, others[1], 88, 101), (others[0], others[1], 100, 99)):
            add(eng, mid, sport, home, away, when, hs=hs, as_=as_)
            mid += 1
            when += timedelta(days=1)
    return mid, when


class EloMathTests(unittest.TestCase):
    def test_expected_is_symmetric_and_bounded(self):
        self.assertAlmostEqual(team_elo.expected(0), 0.5)
        self.assertAlmostEqual(team_elo.expected(100) + team_elo.expected(-100), 1.0)
        self.assertGreater(team_elo.expected(200), 0.75)

    def test_winner_gains_what_loser_loses(self):
        p = team_elo.PARAMS["basketball"]
        r, played = team_elo.run_ratings(
            [{"home": "A", "away": "B", "home_score": 100, "away_score": 90, "start_time": T0}], p)
        self.assertGreater(r["A"], team_elo.MEAN)
        self.assertAlmostEqual(r["A"] + r["B"], 2 * team_elo.MEAN)
        self.assertEqual(played, {"A": 1, "B": 1})

    def test_new_season_pulls_toward_mean_even_before_first_game(self):
        p = team_elo.PARAMS["american_football"]
        g = [{"home": "A", "away": "B", "home_score": 40, "away_score": 0, "start_time": T0}]
        same, _ = team_elo.run_ratings(g, p, as_of=T0 + timedelta(days=7))
        later, _ = team_elo.run_ratings(g, p, as_of=T0 + timedelta(days=200))
        self.assertLess(abs(later["A"] - team_elo.MEAN), abs(same["A"] - team_elo.MEAN))

    def test_model_versions_are_sport_specific(self):
        self.assertEqual(set(team_elo.MODEL_VERSION), set(team_elo.PARAMS))
        self.assertEqual(len(set(team_elo.MODEL_VERSION.values())), 3)


class ForecastEndpointTests(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        app = FastAPI()
        app.include_router(details.router)
        self.client = TestClient(app)
        self.patch = patch.object(details, "engine", self.eng)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def get(self, mid):
        r = self.client.get(f"/v1/matches/{mid}/forecast")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_basketball_strong_team_favoured_with_consumer_shape(self):
        mid, when = season(self.eng, 2, 10)
        add(self.eng, 999, 2, 20, 10, when + timedelta(days=1), status="NS")
        body = self.get(999)
        self.assertTrue(body["available"])
        self.assertEqual(body["model_version"], "elo-basketball-v0.1")
        probs = body["probabilities"]
        self.assertEqual(set(probs), {"home", "draw", "away"})
        self.assertEqual(probs["draw"], 0.0)
        self.assertAlmostEqual(probs["home"] + probs["away"], 1.0, places=3)
        self.assertGreater(probs["away"], 0.7)          # strong team is away here
        self.assertEqual(body["samples"], {"home": 20, "away": 20})
        self.assertNotIn("most_likely_score", body)     # no score model for these sports
        self.assertIn("not calibrated or validated", body["disclaimer"])

    def test_hockey_and_nfl_use_their_own_versions(self):
        for sport, version in ((3, "elo-hockey-v0.1"), (4, "elo-american-football-v0.1")):
            base = sport * 1000
            _, when = season(self.eng, sport, 10, start_id=base, strong=base + 1, weak=base + 2,
                             others=(base + 3, base + 4))
            add(self.eng, base + 999, sport, base + 1, base + 2, when, status="NS")
            body = self.get(base + 999)
            self.assertTrue(body["available"], body)
            self.assertEqual(body["model_version"], version)
            self.assertGreater(body["probabilities"]["home"], 0.5)

    def test_only_earlier_games_of_the_same_sport_count(self):
        _, when = season(self.eng, 2, 10)
        add(self.eng, 900, 2, 10, 20, when, status="NS")
        before = self.get(900)
        # A later result and another sport's games with the same team ids change nothing.
        add(self.eng, 901, 2, 20, 10, when + timedelta(days=2), hs=150, as_=60)
        season(self.eng, 3, 10, start_id=2000, strong=20, weak=10)
        self.assertEqual(self.get(900)["probabilities"], before["probabilities"])

    def test_unfinished_or_scoreless_games_are_ignored(self):
        _, when = season(self.eng, 2, 10)
        add(self.eng, 800, 2, 10, 20, when - timedelta(hours=1), status="PST")
        add(self.eng, 801, 2, 10, 20, when - timedelta(hours=2), status="FT")   # no score
        add(self.eng, 900, 2, 10, 20, when, status="NS")
        self.assertEqual(self.get(900)["samples"], {"home": 20, "away": 20})

    def test_too_little_history_is_unavailable(self):
        season(self.eng, 2, 1)   # two games per team, eight required
        add(self.eng, 900, 2, 10, 20, T0 + timedelta(days=30), status="NS")
        body = self.get(900)
        self.assertFalse(body["available"])
        self.assertIn("reason", body)
        self.assertNotIn("probabilities", body)

    def test_other_sports_still_unavailable(self):
        add(self.eng, 900, 5, 1, 2, T0, status="NS")
        body = self.get(900)
        self.assertFalse(body["available"])
        self.assertEqual(body["sport"], "tennis")

    def test_missing_match_is_404(self):
        self.assertEqual(self.client.get("/v1/matches/12345/forecast").status_code, 404)


if __name__ == "__main__":
    unittest.main()
