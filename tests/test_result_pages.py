"""/livescore/{YYYY-MM-DD} daily results (scratch SQLite, ESPN mocked)."""
import os
import re
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from sitemap_util import all_locs  # noqa: E402

from app import sportiq_datacenter as dc  # noqa: E402
from app import sportiq_results_pages as rp  # noqa: E402
from app import sportiq_seo as seo  # noqa: E402
from app import sportiq_team_pages as tp  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)
TODAY = date(2026, 11, 25)


def ev(eid, when, home, away, hs, as_, state="post", hid="1", aid="2"):
    def c(tid, name, ha, sc):
        return {"homeAway": ha, "team": {"id": tid, "displayName": name}, "score": str(sc)}
    return {"id": eid, "date": when, "competitions": [{
        "date": when, "status": {"type": {"state": state, "completed": state == "post", "shortDetail": "FT"}},
        "competitors": [c(hid, home, "home", hs), c(aid, away, "away", as_)]}]}


# 2026-11-22 KST: EPL games (UTC 22nd afternoon -> KST 22nd night) and NBA (UTC 22nd -> KST 23rd morning? no)
EPL_1121 = {"events": [ev("a1", "2026-11-22T12:00Z", "Arsenal", "Chelsea", 2, 1, hid="359", aid="363")]}   # KST 22 21:00
EPL_1122 = {"events": [
    ev("a2", "2026-11-22T14:30Z", "Liverpool", "Everton", 3, 0, hid="364", aid="368"),                    # KST 22 23:30
    ev("a3", "2026-11-22T16:00Z", "Fulham", "Brentford", 1, 1),                                           # KST 23 01:00 -> other day
    ev("a4", "2026-11-22T13:00Z", "Spurs X", "Leeds X", 0, 0, state="pre"),                               # not finished
]}
NBA_1121 = {"events": [ev("n%d" % i, "2026-11-22T00:30Z", "Boston Celtics", "Miami Heat", 100 + i, 99) for i in range(4)]}   # KST 22 09:30


def fake_fetch(url, timeout=6):
    if "soccer/eng.1/scoreboard?dates=20261121" in url:
        return EPL_1121
    if "soccer/eng.1/scoreboard?dates=20261122" in url:
        return EPL_1122
    if "basketball/nba/scoreboard?dates=20261121" in url:
        return NBA_1121
    if "/scoreboard" in url:
        return {"events": []}
    if url.endswith("/soccer/eng.1/standings"):
        return {"standings": {"entries": [
            {"team": {"id": "359", "displayName": "Arsenal", "shortDisplayName": "Arsenal", "abbreviation": "ARS"},
             "stats": [{"name": "gamesPlayed", "value": 11}, {"name": "points", "value": 28}]}]}}
    return None


class ResultPagesTests(unittest.TestCase):
    def setUp(self):
        tp._CACHE.clear()
        rp._MEM.clear()
        rp._ensure_table()
        with rp._conn() as c:
            c.execute(f"DELETE FROM {rp.TABLE}")
        self.patches = [patch.object(dc, "_fetch_json", side_effect=fake_fetch),
                        patch.object(rp, "today_kst", return_value=TODAY)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_day_page_lists_finished_games_of_that_kst_day(self):
        r = CLIENT.get("/livescore/2026-11-22")
        self.assertEqual(r.status_code, 200)
        t = r.text
        self.assertIn("<title>2026년 11월 22일 스포츠 경기 결과·최종 스코어 | SportIQ</title>", t)
        self.assertIn('<link rel="canonical" href="https://billnojay.com/livescore/2026-11-22" />', t)
        self.assertIn("<h1>2026년 11월 22일 경기 결과</h1>", t)
        self.assertIn("프리미어리그 (2경기)", t)
        self.assertIn("NBA (4경기)", t)
        self.assertNotIn("풀럼", t)            # starts 23rd 01:00 KST
        self.assertNotIn("Spurs X", t)         # not finished
        self.assertIn("2 : 1", t)
        self.assertIn('<a href="/team/epl/arsenal">아스널</a>', t)
        self.assertIn("끝난 6경기", t)
        self.assertIn('href="/livescore/2026-11-21"', t)
        self.assertIn('href="/livescore/2026-11-23"', t)
        self.assertIn("index, follow", t)
        for word in ("예정", "프리뷰", "예상", "배당"):
            self.assertNotIn(word, t)

    def test_stored_once_and_not_refetched(self):
        CLIENT.get("/livescore/2026-11-22")
        self.assertEqual(rp.stored_counts().get("2026-11-22"), 6)
        def no_scoreboards(url, timeout=6):
            if "/scoreboard" in url:
                raise AssertionError("scoreboard refetched")
            return fake_fetch(url, timeout)
        with patch.object(dc, "_fetch_json", side_effect=no_scoreboards):
            r = CLIENT.get("/livescore/2026-11-22")
        self.assertIn("NBA (4경기)", r.text)

    def test_table_is_in_pg_snapshot_list(self):
        from app import sportiq_auto_analyst as aa
        self.assertIn(rp.TABLE, aa._PG_SNAPSHOT_TABLES)

    def test_few_games_noindex_and_not_in_sitemap(self):
        r = CLIENT.get("/livescore/2026-11-20")      # all scoreboards empty
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("x-robots-tag"), seo.NOINDEX)
        CLIENT.get("/livescore/2026-11-22")
        self.assertEqual(rp.sitemap_result_days(), ["2026-11-22"])

    def test_today_redirects_future_and_old_404(self):
        r = CLIENT.get("/livescore/2026-11-25", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (301, "/livescore"))
        for d in ("2026-11-26", "2026-09-20", "2026-13-01", "20261122", "abc"):
            r = CLIENT.get(f"/livescore/{d}")
            self.assertEqual(r.status_code, 404, d)
            self.assertEqual(r.headers.get("x-robots-tag"), seo.NOINDEX)
        self.assertEqual(CLIENT.get("/livescore/2026-09-26").status_code, 200)   # 60th day

    def test_window_is_last_60_days(self):
        w = rp.window(TODAY)
        self.assertEqual((len(w), w[0], w[-1]), (60, date(2026, 11, 24), date(2026, 9, 26)))

    def test_sitemap_has_result_days(self):
        CLIENT.get("/livescore/2026-11-22")
        with patch.object(tp, "sitemap_team_urls", return_value=[]):
            locs = all_locs(CLIENT)
        self.assertIn("https://billnojay.com/livescore/2026-11-22", locs)
        self.assertNotIn("https://billnojay.com/livescore/2026-11-20", locs)

    def test_livescore_links_recent_days(self):
        seo._PAGE_CACHE.clear()
        with patch.object(dc, "get_live_scores", return_value={"matches": [], "server_now_kst": ""}):
            t = CLIENT.get("/livescore").text
        self.assertIn('href="/livescore/2026-11-24"', t)
        self.assertIn('href="/livescore/2026-11-18"', t)


if __name__ == "__main__":
    unittest.main()


class SitemapIndexTests(unittest.TestCase):
    def test_index_lists_three_sitemaps(self):
        r = CLIENT.get("/sitemap.xml")
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/xml", r.headers["content-type"])
        subs = re.findall(r"<loc>(.*?)</loc>", r.text)
        self.assertEqual(subs, [f"https://billnojay.com/sitemap-{k}.xml" for k in ("pages", "teams", "results")])
        self.assertIn("<sitemapindex", r.text)

    def test_sub_sitemaps_split_by_kind(self):
        with patch.object(tp, "sitemap_team_urls", return_value=[("/team/epl/arsenal", "epl")]), \
             patch.object(rp, "sitemap_result_days", return_value=["2026-11-22"]):
            pages = re.findall(r"<loc>(.*?)</loc>", CLIENT.get("/sitemap-pages.xml").text)
            teams = re.findall(r"<loc>(.*?)</loc>", CLIENT.get("/sitemap-teams.xml").text)
            results = CLIENT.get("/sitemap-results.xml").text
        self.assertIn("https://billnojay.com/", pages)
        self.assertIn("https://billnojay.com/livescore", pages)
        self.assertFalse([u for u in pages if "/team/" in u or "/livescore/" in u])
        self.assertEqual(teams, ["https://billnojay.com/team/epl/arsenal"])
        self.assertIn("<loc>https://billnojay.com/livescore/2026-11-22</loc>", results)
        self.assertIn("<lastmod>2026-11-23</lastmod>", results)   # final the day after

    def test_robots_points_to_index(self):
        self.assertIn("Sitemap: https://billnojay.com/sitemap.xml", CLIENT.get("/robots.txt").text)
