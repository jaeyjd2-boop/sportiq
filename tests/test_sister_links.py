"""billnojay match -> same match on zynktv / ergrow (DB and network mocked)."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_sister_links as sl  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)
KST = timezone(timedelta(hours=9))


class SlugTests(unittest.TestCase):
    # Same examples as sports-creator-ai post_slugs (ergrow auto drafts).
    def test_slug_rule_matches_ergrow_drafts(self):
        self.assertEqual(sl.ergrow_slugs(24917, "football", "Manchester City FC", "SSC Napoli"),
                         ["manchester-city-vs-napoli-football-preview-24917", "sca-auto-football-24917"])
        self.assertEqual(sl.ergrow_slugs(42679, "hockey", "Boston Bruins", "Winnipeg Jets")[0],
                         "boston-bruins-vs-winnipeg-jets-hockey-preview-42679")
        self.assertEqual(sl.slug_part("FK Bodø/Glimt"), "bodo-glimt")
        self.assertEqual(sl.ergrow_slugs(7, "american_football", "A", "B")[0], "a-vs-b-american-football-preview-7")

    def test_parse_kickoff(self):
        self.assertEqual(sl.parse_kickoff("2026.11.26 (목) 08:20 KST"), datetime(2026, 11, 26, 8, 20, tzinfo=KST))
        self.assertIsNone(sl.parse_kickoff(""))


class FindTests(unittest.TestCase):
    def test_espn_sport_maps_by_provider_id(self):
        calls = []

        def rows(sql, params):
            calls.append(params)
            return [{"id": 43985, "sport": "basketball", "home": "Brooklyn Nets", "away": "Chicago Bulls"}]
        with patch.object(sl, "_rows", side_effect=rows):
            hit = sl.find_match("NBA-401909317", "basketball", None, "", "")
        self.assertEqual(hit["id"], 43985)
        self.assertEqual(calls[0]["pid"], "espn:401909317")

    def test_football_matches_by_kickoff_and_name(self):
        cands = [{"id": 1, "sport": "football", "home": "Club Atlético de Madrid", "away": "Viking FK"},
                 {"id": 2, "sport": "football", "home": "Inter", "away": "VfB Stuttgart"}]
        ko = datetime(2026, 11, 26, 5, 0, tzinfo=KST)
        with patch.object(sl, "_rows", return_value=cands), \
             patch("app.sportiq_team_pages.korean_name",
                   side_effect=lambda n, s: {"Club Atlético de Madrid": "아틀레티코 마드리드", "Viking FK": "바이킹"}.get(n)):
            self.assertEqual(sl.find_match("UCL-401915291", "soccer", ko, "아틀레티코 마드리드", "바이킹")["id"], 1)
            self.assertIsNone(sl.find_match("UCL-1", "soccer", ko, "모르는 팀", "다른 팀"))

    def test_ambiguous_names_give_no_link(self):
        cands = [{"id": 1, "home": "Arsenal", "away": "X"}, {"id": 2, "home": "Arsenal", "away": "Y"}]
        with patch("app.sportiq_team_pages.korean_name", return_value=None):
            self.assertIsNone(sl.pick_by_names(cands, "Arsenal", "Z", "soccer"))
            self.assertEqual(sl.pick_by_names(cands, "Arsenal", "Y", "soccer")["id"], 2)

    def test_unsupported_sport(self):
        self.assertIsNone(sl.find_match("WTA-1", "tennis", None, "a", "b"))


class LinkTests(unittest.TestCase):
    def setUp(self):
        sl._CACHE.clear()

    def test_ergrow_only_published_same_site_post(self):
        def http(url, timeout=4.0):
            self.assertIn("slug=boston-bruins-vs-winnipeg-jets-hockey-preview-42679%2Csca-auto-hockey-42679", url)
            return [{"slug": "boston-bruins-vs-winnipeg-jets-hockey-preview-42679",
                     "link": "https://ergrow.com/boston-bruins-vs-winnipeg-jets-hockey-preview-42679/"}]
        with patch.object(sl, "_http_json", side_effect=http):
            self.assertEqual(sl.ergrow_preview(42679, "hockey", "Boston Bruins", "Winnipeg Jets"),
                             "https://ergrow.com/boston-bruins-vs-winnipeg-jets-hockey-preview-42679/")
        sl._CACHE.clear()
        with patch.object(sl, "_http_json", return_value=[{"slug": "sca-auto-hockey-1", "link": "https://evil.example/x"}]):
            self.assertIsNone(sl.ergrow_preview(1, "hockey", "A", "B"))
        sl._CACHE.clear()
        with patch.object(sl, "_http_json", side_effect=OSError("down")):
            self.assertIsNone(sl.ergrow_preview(2, "hockey", "A", "B"))

    def test_api_found_and_fallback_to_day_page(self):
        hit = {"id": 43985, "sport": "basketball", "home": "Brooklyn Nets", "away": "Chicago Bulls"}
        today = datetime.now(KST)
        kst = today.strftime("%Y.%m.%d (목) 09:00 KST")
        with patch.object(sl, "find_match", return_value=hit), patch.object(sl, "ergrow_preview", return_value=None):
            d = CLIENT.get("/api/sister/match-links", params={"id": "NBA-1", "sport": "basketball", "kst": kst}).json()
        self.assertEqual(d["zynktv"], "https://zynktv.com/match/43985")
        self.assertIsNone(d["ergrow"])
        sl._CACHE.clear()
        with patch.object(sl, "find_match", return_value=None):
            d = CLIENT.get("/api/sister/match-links", params={"id": "SOCCER_ALL-9", "sport": "soccer", "kst": kst}).json()
        self.assertIsNone(d["zynktv"])
        self.assertEqual(d["zynktv_day"], f"https://zynktv.com/schedule/{today.date().isoformat()}")

    def test_db_error_does_not_break(self):
        with patch.object(sl, "find_match", side_effect=RuntimeError("db down")):
            d = sl.links_for("NBA-1", "basketball", "", "", "")
        self.assertEqual(d, {"zynktv": None, "zynktv_day": None, "ergrow": None})

    def test_zynktv_day_range(self):
        t = date(2026, 11, 26)
        k = lambda d: datetime(d.year, d.month, d.day, 12, 0, tzinfo=KST)
        self.assertIsNotNone(sl.zynktv_day(k(t - timedelta(days=30)), t))
        self.assertIsNone(sl.zynktv_day(k(t - timedelta(days=31)), t))
        self.assertIsNotNone(sl.zynktv_day(k(t + timedelta(days=7)), t))
        self.assertIsNone(sl.zynktv_day(k(t + timedelta(days=8)), t))


class UiTests(unittest.TestCase):
    def test_modals_load_sister_links(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "app", "sportiq_datacenter_ui.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("/api/sister/match-links?", js)
        self.assertIn("window.__sqLoadSisterLinks=loadSisterLinks", js)
        from app import sportiq_lounge_router as lr
        with open(lr.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("window.__sqLoadSisterLinks(it, sqSisterBox)", src)


if __name__ == "__main__":
    unittest.main()
