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

    def test_english_live_names_match_football_data_names(self):
        # Live items keep English names when no Korean name is known; football-data adds "FC".
        cands = [{"id": 7, "home": "Arsenal FC", "away": "Chelsea FC"},
                 {"id": 8, "home": "Fulham FC", "away": "Brentford FC"}]
        with patch("app.sportiq_team_pages.korean_name", return_value=None):
            self.assertEqual(sl.pick_by_names(cands, "Arsenal", "Chelsea", "soccer")["id"], 7)
            self.assertEqual(sl.pick_by_names(cands, "AFC Fulham", "Brentford", "soccer")["id"], 8)

    def test_korean_on_live_side_and_english_on_db_side(self):
        ko = {"Arsenal": "아스널", "Arsenal FC": "아스널", "Chelsea FC": "첼시"}
        cands = [{"id": 7, "home": "Arsenal FC", "away": "Chelsea FC"}]
        with patch("app.sportiq_team_pages.korean_name", side_effect=lambda n, s: ko.get(n)):
            self.assertEqual(sl.pick_by_names(cands, "아스널", "Chelsea", "soccer")["id"], 7)
        # one English live name translated on the live side
        cands = [{"id": 9, "home": "아스널", "away": "X"}]
        with patch("app.sportiq_team_pages.korean_name", side_effect=lambda n, s: ko.get(n)):
            self.assertEqual(sl.pick_by_names(cands, "Arsenal", "", "soccer")["id"], 9)

    def test_club_tokens_do_not_merge_different_teams(self):
        cands = [{"id": 1, "home": "Manchester City FC", "away": "A"}, {"id": 2, "home": "Manchester United FC", "away": "B"}]
        with patch("app.sportiq_team_pages.korean_name", return_value=None):
            self.assertEqual(sl.pick_by_names(cands, "Manchester United", "", "soccer")["id"], 2)

    def test_one_matching_side_is_not_enough(self):
        # Real Korean names: "Arsenal U21" is looked up as 아스널, so one side alone would
        # link a youth match to Arsenal's senior match in the same kickoff window.
        cands = [{"id": 1, "home": "Arsenal FC", "away": "Brentford FC"}]
        self.assertIsNone(sl.pick_by_names(cands, "Arsenal U21", "Chelsea U21", "soccer"))
        self.assertIsNone(sl.pick_by_names(cands, "아스널", "첼시", "soccer"))
        self.assertEqual(sl.pick_by_names(cands, "아스널", "Brentford", "soccer")["id"], 1)

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

    def test_cache_key_includes_names(self):
        kst = "2026.11.26 (목) 05:00 KST"
        calls = []

        def find(live_id, sport, kickoff, home, away):
            calls.append(home)
            return {"id": 29523, "sport": "football", "home": "Club Brugge", "away": "Liverpool"} if home == "클뤼프 브뤼허" else None
        with patch.object(sl, "find_match", side_effect=find), patch.object(sl, "ergrow_preview", return_value=None):
            bad = sl.links_for("UCL-1", "soccer", kst, "made up", "names")      # cached as "not found"
            good = sl.links_for("UCL-1", "soccer", kst, "클뤼프 브뤼허", "리버풀")
        self.assertIsNone(bad["zynktv"])
        self.assertEqual(good["zynktv"], "https://zynktv.com/match/29523")
        self.assertEqual(calls, ["made up", "클뤼프 브뤼허"])

    def test_espn_cache_key_ignores_names(self):
        calls = []

        def find(*a):
            calls.append(a)
            return None
        with patch.object(sl, "find_match", side_effect=find):
            sl.links_for("NBA-1", "basketball", "", "a", "b")
            sl.links_for("NBA-1", "basketball", "", "c", "d")
        self.assertEqual(len(calls), 1)

    def test_recently_used_key_survives_eviction(self):
        with patch.object(sl, "MAX_CACHE", 8):
            for i in range(8):
                sl._cput(f"k{i}", 60, i)
            sl._cget("k0")
            sl._cput("new", 60, "x")
        self.assertEqual(sl._cget("k0"), (True, 0))
        self.assertEqual(sl._cget("k1"), (False, None))

    def test_full_cache_drops_only_the_oldest(self):
        with patch.object(sl, "MAX_CACHE", 8):
            for i in range(8):
                sl._cput(f"k{i}", 60, i)
            sl._cput("new", 60, "x")
        self.assertEqual(sl._cget("k0"), (False, None))     # oldest quarter gone
        self.assertEqual(sl._cget("k7"), (True, 7))         # the rest kept
        self.assertEqual(sl._cget("new"), (True, "x"))

    def test_db_error_does_not_break(self):
        with patch.object(sl, "find_match", side_effect=RuntimeError("db down")):
            d = sl.links_for("NBA-1", "basketball", "", "", "")
        self.assertEqual(d, {"zynktv": None, "zynktv_day": None, "ergrow": None, "gyeonggipan": None})

    def test_gyeonggipan_link(self):
        now = datetime(2026, 11, 28, 3, 0, tzinfo=timezone.utc)   # 12:00 KST
        k = lambda days: datetime(2026, 11, 28, 19, 0, tzinfo=KST) + timedelta(days=days)
        hit = {"id": 118636}
        # In 경기판's build window: its page for that match.
        self.assertEqual(sl.gyeonggipan_link(hit, k(0), now), "https://gyeonggipan.com/match/118636/")
        self.assertEqual(sl.gyeonggipan_link(hit, k(-1), now), "https://gyeonggipan.com/match/118636/")
        self.assertEqual(sl.gyeonggipan_link(hit, k(6), now), "https://gyeonggipan.com/match/118636/")
        # No DB match but still to come this week: the week page.
        self.assertEqual(sl.gyeonggipan_link(None, k(2), now), "https://gyeonggipan.com/this-week/")
        # Outside the window, or unknown kickoff: nothing.
        self.assertIsNone(sl.gyeonggipan_link(hit, k(7), now))
        self.assertIsNone(sl.gyeonggipan_link(hit, k(-2), now))
        self.assertIsNone(sl.gyeonggipan_link(None, k(-1), now))
        self.assertIsNone(sl.gyeonggipan_link(hit, None, now))

    def test_zynktv_day_range(self):
        t = date(2026, 11, 26)
        k = lambda d: datetime(d.year, d.month, d.day, 12, 0, tzinfo=KST)
        self.assertIsNotNone(sl.zynktv_day(k(t - timedelta(days=30)), t))
        self.assertIsNone(sl.zynktv_day(k(t - timedelta(days=31)), t))
        self.assertIsNotNone(sl.zynktv_day(k(t + timedelta(days=7)), t))
        self.assertIsNone(sl.zynktv_day(k(t + timedelta(days=8)), t))


class HomeLinksTests(unittest.TestCase):
    def test_home_links_point_to_gyeonggipan_indexable_pages(self):
        from app import sportiq_seo
        html = sportiq_seo.home_links_html()
        for path in ("/this-week/", "/previews/", "/guides/"):
            self.assertIn(f'href="https://gyeonggipan.com{path}"', html)
        self.assertNotIn("gyeonggipan.com/match/", html)   # 경기판 match pages are noindex


class UiTests(unittest.TestCase):
    def test_modals_load_sister_links(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "app", "sportiq_datacenter_ui.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("/api/sister/match-links?", js)
        self.assertIn("window.__sqLoadSisterLinks=loadSisterLinks", js)
        # A late answer for a closed modal must not fill the next match's box.
        i = js.index("function loadSisterLinks(m,box){")
        body = js[i:js.index("window.__sqLoadSisterLinks", i)]
        self.assertLess(body.index("box=box||document.getElementById('dcSisterLinks')"), body.index("fetch("))
        self.assertIn("!box.isConnected", body)
        from app import sportiq_lounge_router as lr
        with open(lr.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("window.__sqLoadSisterLinks(it, sqSisterBox)", src)


if __name__ == "__main__":
    unittest.main()
