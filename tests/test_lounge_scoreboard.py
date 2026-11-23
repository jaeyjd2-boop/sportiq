"""Lounge scoreboard-v30: ESPN supplement, AI sample labelling, restored fixes.

No network: the datacenter ESPN fetch is patched with fixture-parsed items.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from app import sportiq_datacenter as dc  # noqa: E402
from app import sportiq_lounge_router as lr  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "espn"
SRC = Path(lr.__file__).read_text(encoding="utf-8")


def nfl_items():
    data = json.loads((FIX / "football_nfl.json").read_text(encoding="utf-8"))
    sport, _, full = dc.LEAGUE_CONFIG["NFL"]
    now_iso = dc._now_kst().isoformat()
    return [dict(m, start_kst_iso=now_iso) for m in dc._parse_espn_scoreboard_events(data, "NFL", sport, full)]


class AiLabelTests(unittest.TestCase):
    def test_name_hash_numbers_are_marked_sample(self):
        for sport in ("soccer", "basketball"):
            ai = lr._ai_probs_with_real_odds_v4616("A", "B", sport)
            self.assertTrue(ai["is_sample"])
            self.assertEqual(ai["basis"], "name_hash")

    def test_bookmaker_odds_are_not_sample(self):
        ai = lr._ai_probs_with_real_odds_v4616("A", "B", "soccer", (2.1, 3.3, 3.6))
        self.assertFalse(ai["is_sample"])
        self.assertEqual(ai["basis"], "bookmaker_odds")


class SupplementTests(unittest.TestCase):
    def test_nfl_added_with_no_ai_numbers_and_verified_flags(self):
        existing = [{"id": "NBA-1", "sport": "basketball", "home": "A", "away": "B", "state": "in",
                     "match_dt_kst": "11.23 (월) 09:00", "ai": lr._ai_probs_with_real_odds_v4616("A", "B", "basketball")}]
        with patch.object(dc, "_fetch_espn_scoreboard", side_effect=lambda lg, off=0: nfl_items() if lg == "NFL" else []):
            dc._CACHE.pop("espn_supplement_0", None)
            out = lr._with_espn_supplement_v50(existing, 0)
        nfl = [x for x in out if x["sport"] == "american_football"]
        self.assertTrue(nfl)
        self.assertIs(out[[x["id"] for x in out].index("NBA-1")], existing[0])
        for it in nfl:
            self.assertEqual(it["ai"], {"basis": "none", "is_sample": True})
            self.assertTrue(it["verified_official"])
            self.assertIsInstance(it["period_scores"], str)
            self.assertIsInstance(it["home_score"], int)
        self.assertEqual(out[0]["state"], "in")

    def test_supplement_failure_keeps_items(self):
        items = [{"id": "x", "sport": "soccer"}]
        with patch.object(dc, "_supplement_espn_matches", side_effect=RuntimeError("down")):
            self.assertEqual(lr._with_espn_supplement_v50(items, 0), items)

    def test_counts_know_american_football(self):
        counts = lr._recompute_sport_counts_v4615([{"sport": "american_football", "state": "in"}], {})
        self.assertEqual(counts["american_football"], 1)
        self.assertEqual(counts["soccer"], 0)


class FrontEndSourceTests(unittest.TestCase):
    """The JS lives inside this Python file; check the fixes are in it."""

    def test_nfl_sport_key_and_tab(self):
        self.assertIn('if (s === "american_football" || s === "nfl"', SRC)
        self.assertIn('{ key: "american_football", icon: "🏈", label: "NFL"', SRC)

    def test_cards_hide_example_ai(self):
        self.assertIn("var aiReal = !ai.is_sample && ai.home_pct != null;", SRC)
        self.assertIn('is_sample: !(rawAi.basis === "bookmaker_odds" && rawAi.is_sample === false)', SRC)
        self.assertIn("if (ai.is_sample || ai.home_pct == null) return null;", SRC)

    def test_restored_fixes_present(self):
        for marker in ("def _gm_vote_already_cast", "guardIds", "stSampleHtml", "부상자 데이터 미연동",
                       '"is_sample": str(pid).startswith("mp_seed_")', "votes_include_seed_baseline"):
            self.assertIn(marker, SRC, marker)


if __name__ == "__main__":
    unittest.main()
