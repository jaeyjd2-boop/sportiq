"""NBA/NHL/NFL standings: all teams, by conference, ranked inside the group.

Fixtures are trimmed copies of ESPN's standings responses (2026-11-24).
No network: _fetch_json is patched.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from app import sportiq_datacenter as dc  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "espn_standings"
FILES = {"NBA": "basketball_nba", "NHL": "hockey_nhl", "NFL": "football_nfl"}


def payload(lg):
    return json.loads((FIX / f"{FILES[lg]}.json").read_text(encoding="utf-8"))


def standings(lg, data=None):
    dc._CACHE.pop(f"standings_{lg}", None)
    with patch.object(dc, "_fetch_json", return_value=data if data is not None else payload(lg)):
        return dc.get_standings(lg)


class UsStandingsTests(unittest.TestCase):
    def test_all_teams_split_by_conference(self):
        for lg, groups, size in (("NBA", ["동부", "서부"], 15), ("NHL", ["동부", "서부"], 16), ("NFL", ["AFC", "NFC"], 16)):
            res = standings(lg)
            rows = res["standings"]
            self.assertTrue(res["grouped"], lg)
            self.assertEqual(res["groups"], groups, lg)
            self.assertEqual(len(rows), 2 * size, lg)
            for g in groups:
                gr = [r for r in rows if r["group"] == g]
                self.assertEqual([r["rank"] for r in gr], list(range(1, size + 1)), lg)
            self.assertFalse(res["is_sample"])

    def test_nba_nfl_rank_by_win_pct_with_gp(self):
        for lg in ("NBA", "NFL"):
            rows = standings(lg)["standings"]
            for g in dict.fromkeys(r["group"] for r in rows):
                pcts = [r["pct"] for r in rows if r["group"] == g]
                self.assertEqual(pcts, sorted(pcts, reverse=True), lg)
            for r in rows:
                self.assertEqual(r["gp"], r["w"] + r["l"] + r["d"], (lg, r["team"]))
                self.assertGreater(r["gp"], 0)
                self.assertIsNotNone(r["gb"])
                self.assertNotIn("otl", r)
        east = [r for r in standings("NBA")["standings"] if r["group"] == "동부"]
        self.assertEqual(east[-1]["team"], "Milwaukee Bucks")        # 1-16: last, not first
        self.assertEqual(east[-1]["pts_label"], ".059")

    def test_nhl_points_otl(self):
        rows = standings("NHL")["standings"]
        for g in ("동부", "서부"):
            pts = [r["pts"] for r in rows if r["group"] == g]
            self.assertEqual(pts, sorted(pts, reverse=True))
        car = next(r for r in rows if r["team"] == "Carolina Hurricanes")
        self.assertEqual((car["w"], car["l"], car["otl"], car["pts"], car["gp"]), (18, 3, 3, 39, 24))
        self.assertEqual(car["pts"], 2 * car["w"] + car["otl"])
        west = [r for r in rows if r["group"] == "서부"]
        knights = next(r for r in west if r["team"] == "Vegas Golden Knights")
        stars = next(r for r in west if r["team"] == "Dallas Stars")
        self.assertLess(knights["rank"], stars["rank"])              # 33 pts before 32 pts

    def test_seed_and_zone_from_espn(self):
        nfl = standings("NFL")["standings"]
        self.assertTrue(all(r["seed"] for r in nfl))
        self.assertEqual(sum(1 for r in nfl if r["zone"] == "PO"), 14)   # 7 per conference
        nba = standings("NBA")["standings"]
        self.assertEqual(sum(1 for r in nba if r["zone"] == "PO"), 12)
        self.assertEqual(sum(1 for r in nba if r["zone"] == "PI"), 8)

    def test_unexpected_shape_falls_back_to_labelled_sample(self):
        res = standings("NBA", data={"children": [{"name": "x", "standings": {"entries": [{"bad": 1}]}}]})
        self.assertTrue(res["is_sample"])
        self.assertNotIn("grouped", res)

    def test_soccer_contract_unchanged(self):
        soccer = {"children": [{"standings": {"entries": [
            {"team": {"displayName": "Arsenal", "shortDisplayName": "Arsenal"},
             "stats": [{"name": "gamesPlayed", "value": 12}, {"name": "wins", "value": 9}, {"name": "ties", "value": 2},
                       {"name": "losses", "value": 1}, {"name": "pointsFor", "value": 25}, {"name": "pointsAgainst", "value": 8},
                       {"name": "points", "value": 29}]},
            {"team": {"displayName": "Chelsea", "shortDisplayName": "Chelsea"},
             "stats": [{"name": "gamesPlayed", "value": 12}, {"name": "wins", "value": 7}, {"name": "ties", "value": 3},
                       {"name": "losses", "value": 2}, {"name": "pointsFor", "value": 20}, {"name": "pointsAgainst", "value": 11},
                       {"name": "points", "value": 24}]}]}}]}
        res = standings("EPL", soccer)
        self.assertNotIn("grouped", res)
        r = res["standings"][0]
        for key in ("rank", "team", "gp", "w", "d", "l", "gf", "ga", "gd", "pts_label", "zone", "form"):
            self.assertIn(key, r)
        self.assertNotIn("group", r)
        self.assertEqual((r["rank"], r["team"], r["gp"], r["w"], r["d"], r["l"], r["pts_label"]),
                         (1, "Arsenal", 12, 9, 2, 1, "29점"))

    def test_season_sim_still_runs_on_grouped_rows(self):
        dc._CACHE.pop("standings_NHL", None)
        with patch.object(dc, "_fetch_json", return_value=payload("NHL")):
            sim = dc.get_season_sim("NHL")
        self.assertTrue(sim.get("ok", True))


if __name__ == "__main__":
    unittest.main()
