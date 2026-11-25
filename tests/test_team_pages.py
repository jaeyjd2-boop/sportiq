"""/team/{league}/{slug} pages (scratch SQLite, no network: ESPN mocked)."""
import os
import re
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_datacenter as dc  # noqa: E402
from app import sportiq_seo as seo  # noqa: E402
from app import sportiq_team_pages as tp  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)


def entry(tid, name, short, abbr, gp, w, d, l, pts, gf, ga):
    stats = [{"name": "gamesPlayed", "value": gp}, {"name": "wins", "value": w}, {"name": "ties", "value": d},
             {"name": "losses", "value": l}, {"name": "points", "value": pts},
             {"name": "pointsFor", "value": gf}, {"name": "pointsAgainst", "value": ga}]
    return {"team": {"id": tid, "displayName": name, "shortDisplayName": short, "abbreviation": abbr}, "stats": stats}


EPL_STANDINGS = {"standings": {"entries": [
    entry("359", "Arsenal", "Arsenal", "ARS", 11, 9, 1, 1, 28, 17, 7),
    entry("363", "Chelsea", "Chelsea", "CHE", 11, 5, 1, 5, 16, 15, 15),
    entry("999", "Brighton & Hove Albion", "Brighton", "BHA", 2, 1, 0, 1, 3, 2, 2),   # too few games
    entry("998", "Newtown Rovers", "Newtown", "NEW", 11, 3, 3, 5, 12, 9, 14),         # no Korean name
]}}


def game(eid, date, home_id, home_name, away_id, away_name, hs=None, as_=None, state="post"):
    def comp(tid, name, ha, sc, other):
        c = {"homeAway": ha, "team": {"id": tid, "displayName": name}}
        if sc is not None:
            c["score"] = {"value": float(sc), "displayValue": str(sc)}
            c["winner"] = sc > other
        return c
    return {"id": eid, "date": date, "competitions": [{
        "date": date, "status": {"type": {"state": state}},
        "competitors": [comp(home_id, home_name, "home", hs, as_), comp(away_id, away_name, "away", as_, hs)]}]}


ARS_SCHEDULE = {"events": [
    game("1", "2026-11-01T15:00Z", "359", "Arsenal", "363", "Chelsea", 2, 1),
    game("2", "2026-11-08T15:00Z", "40", "Liverpool", "359", "Arsenal", 0, 1),
    game("3", "2026-11-22T15:00Z", "359", "Arsenal", "361", "Newcastle United", 2, 2),
]}
ARS_FIXTURE = {"events": [
    game("4", "2026-11-29T16:30Z", "359", "Arsenal", "382", "Manchester City", state="pre"),
    game("5", "2026-12-02T20:15Z", "362", "Aston Villa", "359", "Arsenal", state="pre"),
]}


def fake_fetch(url, timeout=6):
    if url.endswith("/soccer/eng.1/standings"):
        return EPL_STANDINGS
    if "/teams/359/schedule?fixture=true" in url:
        return ARS_FIXTURE
    if "/teams/359/schedule" in url:
        return ARS_SCHEDULE
    if "/schedule" in url:
        return {"events": []}
    return None


class TeamPageTests(unittest.TestCase):
    def setUp(self):
        tp._CACHE.clear()
        tp._PAGE_CACHE.clear()
        seo._PAGE_CACHE.clear()
        self.p = patch.object(dc, "_fetch_json", side_effect=fake_fetch)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_slugs_from_english_names(self):
        self.assertEqual(tp.slugify("Brighton & Hove Albion"), "brighton-hove-albion")
        self.assertEqual(tp.slugify("Atlético Madrid"), "atletico-madrid")
        self.assertEqual(tp.slugify("St. Louis CITY SC"), "st-louis-city-sc")
        slugs = {t["slug"] for t in tp.league_teams("epl")}
        self.assertEqual(slugs, {"arsenal", "chelsea", "brighton-hove-albion", "newtown-rovers"})

    def test_page_content(self):
        r = CLIENT.get("/team/epl/arsenal")
        self.assertEqual(r.status_code, 200)
        t = r.text
        self.assertIn("<title>아스널 순위·최근 경기 결과 | 프리미어리그 | SportIQ</title>", t)
        self.assertIn('<link rel="canonical" href="https://billnojay.com/team/epl/arsenal" />', t)
        self.assertIn("<h1>아스널 (Arsenal) — 프리미어리그</h1>", t)
        self.assertIn("1위", t)
        self.assertIn("11경기 9승 1무 1패 · 승점 28", t)
        self.assertIn("index, follow", t)
        self.assertNotIn("x-robots-tag", {k.lower() for k in r.headers})
        # newest result first, final scores
        self.assertLess(t.index("뉴캐슬 유나이티드"), t.index("리버풀"))
        self.assertIn("2 : 2", t)
        self.assertIn("1 : 0", t)
        # exactly one next match, one line, no link, no list
        self.assertEqual(t.count("다음 경기:"), 1)
        self.assertIn("다음 경기: 11월 30일 (월) vs 맨체스터 시티 (홈)</p>", t)
        self.assertNotIn("애스턴 빌라", t)
        self.assertNotIn("zynktv", t)
        # links: league table and other teams, unique description
        self.assertIn('href="/standings/epl"', t)
        self.assertIn('href="/team/epl/chelsea"', t)
        self.assertIn("최근 결과: 뉴캐슬 유나이티드전 2-2 무", t)
        for word in ("예상", "프리뷰", "배당", "분석"):
            self.assertNotIn(word, t)

    def test_too_few_games_is_noindex_and_out_of_sitemap(self):
        r = CLIENT.get("/team/epl/brighton-hove-albion")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("x-robots-tag"), seo.NOINDEX)
        self.assertIn('content="noindex, follow"', r.text)
        paths = [p for p, _ in tp.sitemap_team_urls()]
        self.assertNotIn("/team/epl/brighton-hove-albion", paths)
        self.assertIn("/team/epl/arsenal", paths)

    def test_schedule_failure_makes_page_noindex(self):
        r = CLIENT.get("/team/epl/chelsea")          # schedule mocked empty
        self.assertEqual(r.headers.get("x-robots-tag"), seo.NOINDEX)
        self.assertIn("불러오지 못했습니다", r.text)

    def test_english_name_when_no_korean(self):
        t = CLIENT.get("/team/epl/newtown-rovers").text
        self.assertIn("<title>Newtown Rovers 순위·최근 경기 결과", t)
        self.assertIn("<h1>Newtown Rovers — 프리미어리그</h1>", t)

    def test_unknown_team_or_league_404(self):
        for path in ("/team/epl/nope", "/team/kleague/fc-seoul", "/team/ucl/arsenal"):
            r = CLIENT.get(path)
            self.assertEqual(r.status_code, 404, path)
            self.assertEqual(r.headers.get("x-robots-tag"), seo.NOINDEX)

    def test_sitemap_lists_indexable_teams(self):
        locs = re.findall(r"<loc>(.*?)</loc>", CLIENT.get("/sitemap.xml").text)
        self.assertIn("https://billnojay.com/team/epl/arsenal", locs)
        self.assertNotIn("https://billnojay.com/team/epl/brighton-hove-albion", locs)
        self.assertEqual(len(locs), len(set(locs)))

    def test_standings_table_links_to_team_pages(self):
        with patch.object(dc, "get_standings", return_value={
                "ok": True, "sport": "soccer", "is_sample": False, "source": "ESPN", "updated_at_kst": "x",
                "standings": [{"rank": 1, "team": "Arsenal", "gp": 11, "w": 9, "d": 1, "l": 1, "gd": 10, "_pts": 28}]}):
            t = CLIENT.get("/standings/epl").text
        self.assertIn('<a href="/team/epl/arsenal">아스널</a>', t)

    def test_new_england_is_not_the_national_team(self):
        from app import sportiq_lounge_router as lr
        lr._TEAM_KO_MEMO_V4653.clear()
        self.assertEqual(lr._localize_team_ko_v37("New England Revolution", "soccer"), "뉴잉글랜드 레볼루션")
        self.assertEqual(lr._localize_team_ko_v37("England", "soccer"), "잉글랜드")


if __name__ == "__main__":
    unittest.main()
