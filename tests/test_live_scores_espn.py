"""Live-scores ESPN parsing and sample-exclusion tests.

Fixtures in tests/fixtures/espn are trimmed copies of real ESPN site-API
scoreboard responses (fetched 2026-11-23). No network access in these tests.
"""
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from app import sportiq_datacenter as dc  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "espn"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def parse(name, league):
    sport, _, full = dc.LEAGUE_CONFIG[league]
    return dc._parse_espn_scoreboard_events(load(name), league, sport, full)


class EspnParsingTests(unittest.TestCase):
    def test_nfl_team_scores_and_quarters(self):
        rows = parse("football_nfl", "NFL")
        self.assertTrue(rows)
        live = rows[0]
        self.assertEqual(live["sport"], "american_football")
        self.assertEqual((live["home"], live["away"]), ("49ers", "Vikings"))
        self.assertEqual(live["state"], "in")
        self.assertIsInstance(live["home_score"], int)
        self.assertTrue(all(p.startswith(("1Q", "2Q", "3Q", "4Q", "OT")) for p in rows[1]["period_scores"]))
        self.assertTrue(live["id"].startswith("NFL-"))
        self.assertNotIn("ai", live)

    def test_mlb(self):
        rows = parse("baseball_mlb", "MLB")
        self.assertEqual(rows[0]["sport"], "baseball")
        self.assertEqual(rows[0]["state"], "post")

    def test_ufc_every_bout_with_result_not_score(self):
        rows = parse("mma_ufc", "UFC")
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["id"] for r in rows}), 3)
        for r in rows:
            self.assertEqual((r["home_score"], r["away_score"]), ("", ""))
            if r["state"] == "post":
                self.assertTrue(r["period_scores"][0].startswith("승: "))

    def test_tennis_groupings_doubles_and_singles(self):
        atp = parse("tennis_atp", "ATP")
        wta = parse("tennis_wta", "WTA")
        self.assertTrue(atp and wta)
        doubles = atp[0]
        self.assertIn(" / ", doubles["home"])
        self.assertIn("Doubles", doubles["league_full"])
        self.assertEqual(doubles["home_score"] + doubles["away_score"], len(doubles["period_scores"]))
        self.assertTrue(all(r["period_scores"][0].startswith("1세트 ") for r in wta if r["period_scores"]))

    def test_f1_race_podium(self):
        rows = parse("racing_f1", "F1")
        self.assertEqual(len(rows), 1)
        race = rows[0]
        self.assertTrue(race["status_badge"].startswith("Race"))
        self.assertTrue(race["period_scores"][0].startswith("1위 "))
        self.assertEqual(race["home"], race["period_scores"][0][3:])

    def test_unknown_shapes_yield_empty(self):
        for bad in (None, [], {"events": None}, {"events": [{"competitions": [{"competitors": [{}]}]}]},
                    {"events": ["x", {"groupings": "bad"}]}):
            self.assertEqual(dc._parse_espn_scoreboard_events(bad, "ATP", "tennis", "ATP"), [])


def real_item(i, sport="soccer", state="post"):
    return {"id": f"SOCCER_ALL-{1000 + i}", "sport": sport, "league": "EPL", "state": state,
            "home": f"H{i}", "away": f"A{i}", "home_score": 1, "away_score": 0,
            "ai": {"home_pct": 50, "draw_pct": 20, "away_pct": 30}, "drop_badge": "📉 -12%"}


class LiveScoresTests(unittest.TestCase):
    def call(self, collected, extra=None):
        with patch.object(dc, "_collect_all_matches", return_value=collected), \
                patch.object(dc, "_fetch_espn_scoreboard", side_effect=lambda lg, off=0: list((extra or {}).get(lg, []))):
            dc._CACHE.pop("espn_supplement_0", None)
            return dc.get_live_scores(sport="all", league="all", date_offset=0)

    def test_counts_exclude_samples_and_fake_numbers_removed(self):
        samples = dc._korean_schedule_matches(0) + dc._extra_sports_matches(0, [])
        self.assertTrue(samples)
        res = self.call([real_item(1, state="in"), real_item(2, "basketball")] + samples)
        self.assertTrue(res["ok"])
        c = res["counts"]
        self.assertEqual(c["all"], 2)
        self.assertEqual(c["live"], 1)
        for sp in ("esports", "volleyball", "ufc", "tennis", "f1", "baseball"):
            self.assertEqual(c[sp], 0, sp)
        self.assertEqual(res["sample_matches_count"], len(samples))
        self.assertEqual(res["odds_alerts"], [])
        for m in res["matches"]:
            self.assertNotIn("ai", m)
            self.assertNotIn("drop_badge", m)
        self.assertTrue(all(m["is_sample"] for m in res["matches"] if m.get("source") == "sample"))

    def test_live_filter_excludes_samples(self):
        samples = dc._korean_schedule_matches(0)  # today -> state "in"
        with patch.object(dc, "_collect_all_matches", return_value=[real_item(1, state="in")] + samples), \
                patch.object(dc, "_fetch_espn_scoreboard", return_value=[]):
            dc._CACHE.pop("espn_supplement_0", None)
            res = dc.get_live_scores(sport="live", league="all", date_offset=0)
        self.assertEqual([m["id"] for m in res["matches"]], ["SOCCER_ALL-1001"])

    def test_supplement_adds_missing_sports_dedupes_and_drops_stale(self):
        nfl = parse("football_nfl", "NFL")
        now_iso = dc._now_kst().isoformat()
        fresh = [dict(m, start_kst_iso=now_iso) for m in nfl[:2]]
        fresh.append(dict(nfl[0], id="NFL-999001", home="Rams", away="Jets", start_kst_iso=now_iso))
        stale = dict(nfl[1], id="NFL-999002", home="Old", away="Game", state="post",
                     start_kst_iso=(dc._now_kst() - timedelta(days=9)).isoformat())
        # A lounge item already carrying the same ESPN event id must not be duplicated.
        lounge_dup = dict(real_item(9, "american_football"), id="NFL-" + fresh[1]["id"].split("-")[1])
        res = self.call([lounge_dup], {"NFL": fresh + [stale]})
        ids = [m["id"] for m in res["matches"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(fresh[0]["id"], ids)
        self.assertNotIn(stale["id"], ids)
        self.assertEqual(res["counts"]["american_football"], 3)

    def test_cached_collection_is_not_mutated(self):
        collected = [real_item(1)]
        self.call(collected)
        self.assertIn("ai", collected[0])


if __name__ == "__main__":
    unittest.main()
