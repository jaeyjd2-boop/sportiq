"""/picks: upcoming match cards and "이 경기 더 보기" links to ergrow / zynktv / 경기판 (DB and network mocked)."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_quick_picks as qp  # noqa: E402
from app import sportiq_sister_links as sl  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)
NOW = datetime(2026, 11, 28, 3, 0, tzinfo=timezone.utc)   # 12:00 KST
POSTS = [
    {"slug": "football-tonight-preview-2026-11-28", "link": "https://ergrow.com/football-tonight-preview-2026-11-28/"},
    {"slug": "ipswich-town-vs-aston-villa-football-preview-38147", "link": "https://ergrow.com/ipswich-town-vs-aston-villa-football-preview-38147/"},
    {"slug": "washington-wizards-vs-orlando-magic-basketball-preview-42637", "link": "https://ergrow.com/washington-wizards-vs-orlando-magic-basketball-preview-42637/"},
    {"slug": "x-vs-y-football-preview-99", "link": "https://evil.example/x"},
]
ROWS = [
    {"id": 38147, "sport": "football", "home": "Ipswich Town FC", "away": "Aston Villa FC",
     "start_time": NOW + timedelta(hours=12), "league": "Premier League"},
    {"id": 42637, "sport": "basketball", "home": "Washington Wizards", "away": "Orlando Magic",
     "start_time": NOW + timedelta(hours=30), "league": "NBA"},
]


class CardTests(unittest.TestCase):
    def setUp(self):
        sl._CACHE.clear()

    def test_previews_by_match_id_only_from_ergrow(self):
        with patch.object(sl, "_http_json", return_value=POSTS):
            self.assertEqual(sl.ergrow_recent_previews(), {38147: POSTS[1]["link"], 42637: POSTS[2]["link"]})

    def test_cards_link_all_three_sites(self):
        seen = {}

        def rows(sql, params):
            seen.update(params)
            return ROWS

        with patch.object(sl, "_http_json", return_value=POSTS), patch.object(sl, "_rows", side_effect=rows), \
             patch("app.sportiq_team_pages.korean_name", side_effect=lambda n, s: {"Ipswich Town FC": "입스위치"}.get(n)):
            cards = sl.upcoming_match_cards(now=NOW)
        self.assertEqual(sorted(seen["ids"]), [38147, 42637])
        self.assertEqual([c["id"] for c in cards], [38147, 42637])
        first = cards[0]
        self.assertEqual(first["match_label"], "입스위치 vs Aston Villa FC")
        self.assertEqual(first["pick_league"], "EPL")
        self.assertEqual(first["links"], {"ergrow": POSTS[1]["link"], "zynktv": "https://zynktv.com/match/38147",
                                          "gyeonggipan": "https://gyeonggipan.com/match/38147/"})
        self.assertEqual(cards[1]["pick_league"], "NBA")

    def test_ergrow_down_gives_no_cards(self):
        with patch.object(sl, "_http_json", side_effect=OSError("down")), patch.object(sl, "_rows") as rows:
            self.assertEqual(sl.upcoming_match_cards(now=NOW), [])
        rows.assert_not_called()

    def test_football_names_use_soccer_tables_and_drop_accents(self):
        calls = []

        def ko(name, sport):
            calls.append((name, sport))
            return {"Deportivo Alaves": "알라베스"}.get(name)

        with patch("app.sportiq_team_pages.korean_name", side_effect=ko):
            self.assertEqual(sl._display_name("Deportivo Alavés", "football"), "알라베스")
            self.assertEqual(sl._display_name("Unknown FC", "football"), "Unknown FC")
        self.assertEqual(calls[:2], [("Deportivo Alavés", "soccer"), ("Deportivo Alaves", "soccer")])

    def test_korean_league_labels(self):
        cases = [("football", "Primera Division", "라리가"), ("football", "Premier League", "프리미어리그"),
                 ("football", "UEFA Champions League", "챔피언스리그"), ("football", "UEFA Europa Conference League", "컨퍼런스리그"),
                 ("football", "UEFA Europa League", "유로파리그"), ("football", "Championship", "챔피언십"),
                 ("football", "2. Bundesliga", "2. Bundesliga"), ("football", "Ligue 2", "Ligue 2"),
                 ("basketball", "NBA", "NBA"), ("basketball", "WKBL", "WKBL"), ("basketball", "EuroLeague", "EuroLeague")]
        for sport, name, want in cases:
            self.assertEqual(sl.league_ko(sport, name), want, name)

    def test_league_codes(self):
        self.assertEqual(sl.pick_league_code("football", "Primera Division"), "LALIGA")
        self.assertEqual(sl.pick_league_code("football", "UEFA Champions League"), "UCL")
        self.assertIsNone(sl.pick_league_code("football", "Premier League 2"))
        self.assertIsNone(sl.pick_league_code("football", "Eredivisie"))
        self.assertEqual(sl.pick_league_code("basketball", "KBL"), "KBL")
        self.assertEqual(sl.pick_league_code("hockey", "NHL"), "NHL")


class PickLinkTests(unittest.TestCase):
    def setUp(self):
        sl._CACHE.clear()

    def test_split_label(self):
        self.assertEqual(sl.split_match_label("아스널 vs 첼시"), ("아스널", "첼시"))
        self.assertEqual(sl.split_match_label("Arsenal VS. Chelsea"), ("Arsenal", "Chelsea"))
        self.assertEqual(sl.split_match_label("북런던 더비"), ("", ""))

    def test_pick_finds_match_by_names(self):
        now = datetime.now(timezone.utc)
        cands = [{"id": 7, "sport": "football", "home": "Arsenal FC", "away": "Chelsea FC",
                  "start_time": now + timedelta(days=1)}]
        ko = {"Arsenal FC": "아스널", "Chelsea FC": "첼시"}
        with patch.object(sl, "_rows", return_value=cands), patch.object(sl, "ergrow_preview", return_value=None), \
             patch("app.sportiq_team_pages.korean_name", side_effect=lambda n, s: ko.get(n)):
            links = sl.links_for_pick("EPL", "아스널 vs 첼시", now)
        self.assertEqual(links["zynktv"], "https://zynktv.com/match/7")
        self.assertIsNone(links["ergrow"])
        self.assertEqual(links["gyeonggipan"], "https://gyeonggipan.com/match/7/")

    def test_free_text_or_unknown_league_does_no_lookup(self):
        with patch.object(sl, "_rows") as rows:
            self.assertEqual(sl.links_for_pick("EPL", "북런던 더비", NOW), {"zynktv": None, "ergrow": None, "gyeonggipan": None})
            sl.links_for_pick("XYZ", "A vs B", NOW)
        rows.assert_not_called()


class PageTests(unittest.TestCase):
    def setUp(self):
        sl._CACHE.clear()

    def test_hub_shows_cards_with_write_button(self):
        card = {"id": 38147, "sport": "football", "league": "Premier League", "home": "입스위치", "away": "아스톤 빌라",
                "kickoff": (NOW + timedelta(hours=12)).isoformat(), "pick_league": "EPL", "match_label": "입스위치 vs 아스톤 빌라",
                "links": {"ergrow": "https://ergrow.com/a/", "zynktv": "https://zynktv.com/match/38147", "gyeonggipan": None}}
        with patch.object(sl, "upcoming_match_cards", return_value=[card]), patch.object(qp, "list_recent_picks", return_value=[]):
            html = CLIENT.get("/picks").text
            nba = CLIENT.get("/picks?league=NBA").text
        self.assertIn('id="qpMatchCards"', html)
        self.assertIn('data-write-match="입스위치 vs 아스톤 빌라"', html)
        self.assertIn("https://zynktv.com/match/38147", html)
        self.assertNotIn("시청 가이드 · 경기판", html)          # no link -> no button
        self.assertNotIn('id="qpMatchCards"', nba)               # league filter hides other leagues

    def test_hub_without_cards_has_no_section(self):
        with patch.object(sl, "upcoming_match_cards", side_effect=RuntimeError("x")), patch.object(qp, "list_recent_picks", return_value=[]):
            html = CLIENT.get("/picks").text
        self.assertNotIn("qpMatchCardsTitle", html)
        self.assertIn("qpWriteForm", html)

    def test_hub_prefills_match_from_query(self):
        with patch.object(sl, "upcoming_match_cards", return_value=[]), patch.object(qp, "list_recent_picks", return_value=[]):
            r = CLIENT.get("/picks", params={"league": "NBA", "match": "워싱턴 위저즈  vs 올랜도 매직\"><script>"})
        self.assertIn('value="워싱턴 위저즈 vs 올랜도 매직&quot;&gt;&lt;script&gt;"', r.text)
        self.assertIn('<option value="NBA" selected>', r.text)
        self.assertIn('scrollIntoView({block:"start"})', r.text)
        self.assertIn("noindex", r.headers.get("X-Robots-Tag", ""))

    def test_hub_without_match_has_empty_composer(self):
        with patch.object(sl, "upcoming_match_cards", return_value=[]), patch.object(qp, "list_recent_picks", return_value=[]):
            html = CLIENT.get("/picks").text
        self.assertIn('required value="" />', html)
        self.assertNotIn("scrollIntoView({block", html)

    def test_live_modal_has_pick_write_link(self):
        js = open(os.path.join(os.path.dirname(__file__), "..", "app", "sportiq_datacenter_ui.js"), encoding="utf-8").read()
        self.assertIn("function pickWriteLink(m)", js)
        self.assertIn("m.state==='post'", js)
        self.assertIn("'/picks?league='+encodeURIComponent(lg)+'&match='", js)

    def test_chips_follow_cards_then_live_games(self):
        cards = [{"pick_league": "LALIGA", "match_label": "레알 베티스 vs 라요 바예카노"},
                 {"pick_league": None, "match_label": "아약스 vs PSV"}]
        live = {"matches": [
            {"league_code": "LALIGA", "home": "레알 베티스", "away": "라요 바예카노", "state": "pre"},   # same as a card
            {"league_code": "MLS", "home": "레알 솔트레이크", "away": "Vancouver", "state": "pre"},      # no MLS on /picks
            {"league_code": "NBA", "home": "유타 재즈", "away": "덴버 너기츠", "state": "post"},          # finished
            {"league_code": "VLEAGUE_W", "home": "흥국생명", "away": "현대건설", "state": "pre"},
            {"league_code": "NBA", "home": "LA 클리퍼스", "away": "뉴올리언스 펠리컨스", "state": "in", "is_sample": True},
        ]}
        with patch.object(sl, "upcoming_match_cards", return_value=cards),              patch("app.sportiq_datacenter.get_live_scores", return_value=live):
            chips = qp._today_match_chips()
        self.assertEqual(chips, [("LALIGA", "레알 베티스 vs 라요 바예카노"), ("VLEAGUE", "흥국생명 vs 현대건설")])

    def test_chips_fall_back_when_nothing_is_found(self):
        with patch.object(sl, "upcoming_match_cards", side_effect=RuntimeError("x")),              patch("app.sportiq_datacenter.get_live_scores", side_effect=RuntimeError("y")):
            self.assertEqual(qp._today_match_chips(), qp.FALLBACK_MATCH_CHIPS)

    def test_pick_detail_shows_sister_links(self):
        pick = {"id": 5, "status": "active", "league": "EPL", "match_label": "아스널 vs 첼시", "pick_code": "HOME", "confidence": 4,
                "headline": "아스널 홈승", "line1_reason": "a" * 20, "line2_factor": "b" * 20, "line3_pick": "c" * 20,
                "author_name": "테스터", "author_slug": "tester", "likes_count": 0, "comments_count": 0, "views_count": 0,
                "created_at": "2026-11-28T12:00:00+09:00", "updated_at": None}
        links = {"zynktv": "https://zynktv.com/match/7", "ergrow": "https://ergrow.com/p/", "gyeonggipan": None}
        with patch.object(qp, "get_pick_with_comments", return_value=(pick, [], 1)), \
             patch.object(sl, "links_for_pick", return_value=links) as lf:
            html = CLIENT.get("/picks/5").text
        self.assertIn("이 경기 더 보기", html)
        self.assertIn("https://ergrow.com/p/", html)
        self.assertEqual(lf.call_args[0][:2], ("EPL", "아스널 vs 첼시"))


if __name__ == "__main__":
    unittest.main()
