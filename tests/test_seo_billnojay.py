"""billnojay.com search fixes (2026-11-25 audit).

Scratch SQLite only; no network (datacenter calls are patched).
"""
import os
import re
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_datacenter as dc  # noqa: E402
from app import sportiq_lounge_router as lr  # noqa: E402
from app import sportiq_member_upgrade as mu  # noqa: E402
from app import sportiq_seo as seo  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)


def head_of(text):
    return text[: text.find("</head>")]


def meta(text, attr, name):
    m = re.search(rf'<meta\s+{attr}=["\']{re.escape(name)}["\']\s+content=["\']([^"\']*)', text)
    return m.group(1) if m else None


def fresh():
    seo._PAGE_CACHE.clear()


EPL = {
    "ok": True, "league": "EPL", "sport": "soccer", "source": "ESPN Live API", "is_sample": False,
    "updated_at_kst": "2026.11.25 (수) 11:00 KST",
    "standings": [
        {"rank": 1, "team": "Arsenal", "gp": 11, "w": 9, "d": 1, "l": 1, "gd": 10, "_pts": 28, "pts_label": "28점"},
        {"rank": 2, "team": "Brighton <b>", "gp": 11, "w": 7, "d": 1, "l": 3, "gd": 17, "_pts": 22, "pts_label": "22점"},
    ],
}
NBA = {
    "ok": True, "league": "NBA", "sport": "basketball", "source": "ESPN Live API", "is_sample": False, "grouped": True,
    "updated_at_kst": "x",
    "standings": [
        {"rank": 1, "group": "동부", "team": "Philadelphia 76ers", "gp": 18, "w": 14, "d": 0, "l": 4, "pts_label": ".778"},
        {"rank": 1, "group": "서부", "team": "San Antonio Spurs", "gp": 18, "w": 15, "d": 0, "l": 3, "pts_label": ".833"},
    ],
}
SAMPLE = {"ok": True, "sport": "soccer", "is_sample": True, "standings": [{"rank": 1, "team": "리버풀", "gp": 1}]}
LIVE = {
    "server_now_kst": "2026.11.25 (수) 11:00 KST",
    "matches": [
        {"sport": "basketball", "league": "NBA", "league_full": "미국 프로농구 NBA", "state": "post", "home": "올랜도 매직",
         "away": "디트로이트 피스톤스", "home_score": 86, "away_score": 104, "match_dt_kst": "09:00", "status_badge": "종료", "is_sample": False},
        {"sport": "basketball", "league": "NBA", "league_full": "미국 프로농구 NBA", "state": "in", "home": "워싱턴 위저즈",
         "away": "보스턴 셀틱스", "home_score": 61, "away_score": 86, "match_dt_kst": "10:00", "status_badge": "LIVE 3rd", "is_sample": False},
        {"sport": "soccer", "league": "EPL", "league_full": "EPL", "state": "pre", "home": "예시 FC", "away": "샘플 FC",
         "home_score": 0, "away_score": 0, "match_dt_kst": "23:00", "is_sample": True},
    ],
}


class HomeTitleTests(unittest.TestCase):
    def test_home_head_uses_role_terms(self):
        r = CLIENT.get("/")
        self.assertEqual(r.status_code, 200)
        h = head_of(r.text)
        self.assertIn(f"<title>{seo.HOME_TITLE}</title>", h)
        self.assertIn("라이브스코어", seo.HOME_TITLE)
        self.assertIn("순위표", seo.HOME_TITLE)
        self.assertIn("승부예측", seo.HOME_TITLE)
        self.assertEqual(meta(h, "property", "og:title"), seo.HOME_TITLE)
        self.assertEqual(meta(h, "name", "twitter:title"), seo.HOME_TITLE)
        self.assertIn("라이브스코어", meta(h, "name", "description"))
        self.assertIn(seo.HOME_DESC, h)                                  # JSON-LD description
        self.assertIn(f">{seo.HOME_H1}</h1>", r.text)
        for word in ("분석", "Chart.js", "인사이트 라운지"):
            self.assertNotIn(word, h, word)
        self.assertIn("index, follow", meta(h, "name", "robots"))

    def test_rebrand_maps_old_copies_to_new_title(self):
        out = mu._rebrand_scobi_text("<title>SportIQ 오픈 스포츠 인사이트 라운지</title><p>billnojay.com 오픈 스포츠 인사이트</p>")
        self.assertIn("<title>SportIQ 라이브스코어 · 리그 순위표 · 승부예측 커뮤니티</title>", out)
        self.assertIn("billnojay.com 라이브스코어·순위표·승부예측 커뮤니티", out)
        self.assertNotIn("분석", out)
        self.assertNotIn("실시간 분석 스포츠IQ", mu.UPGRADE_JS if isinstance(getattr(mu, "UPGRADE_JS", ""), str) else "")


class PostPagesTests(unittest.TestCase):
    def test_existing_post_is_noindex(self):
        fake = {"id": "77", "title": "테스트 글", "desc": "요약", "league": "EPL", "author": "a"}
        with patch.object(lr, "resolve_post_og_meta_v29", return_value=fake):
            r = CLIENT.get("/?post=77")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("x-robots-tag"), seo.NOINDEX)
        self.assertEqual(meta(head_of(r.text), "name", "robots"), seo.NOINDEX)
        self.assertEqual(len(re.findall(r'<meta\s+name=["\']robots', r.text)), 1)
        self.assertIn("<title>테스트 글 | SportIQ</title>", r.text)

    def test_unknown_post_is_404_noindex(self):
        r = CLIENT.get("/?post=99999999")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(meta(head_of(r.text), "name", "robots"), seo.NOINDEX)

    def test_sitemap_and_rss_have_no_post_urls(self):
        from sitemap_util import all_locs
        from app import sportiq_team_pages as tp
        with patch.object(tp, "sitemap_team_urls", return_value=[]):
            locs = all_locs(CLIENT)
        self.assertFalse([u for u in locs if "?post=" in u])
        self.assertIn("https://billnojay.com/", locs)
        for path, _f, _p in seo.PUBLIC_PATHS:
            self.assertIn(seo.SITE + path, locs)
        rss = CLIENT.get("/rss.xml").text
        self.assertNotIn("?post=", rss)
        self.assertIn("<link>https://billnojay.com/livescore</link>", rss)
        self.assertIn("<link>https://billnojay.com/standings/epl</link>", rss)
        self.assertNotIn("분석", rss)


class PublicPagesTests(unittest.TestCase):
    def setUp(self):
        fresh()
        from app import sportiq_team_pages as tp   # no ESPN calls for team links here
        self._tp = patch.object(tp, "team_slug_lookup", return_value={})
        self._tp.start()
        self.addCleanup(self._tp.stop)

    def test_livescore_snapshot(self):
        with patch.object(dc, "get_live_scores", return_value=LIVE):
            r = CLIENT.get("/livescore")
        self.assertEqual(r.status_code, 200)
        t = r.text
        self.assertIn('<link rel="canonical" href="https://billnojay.com/livescore" />', t)
        self.assertIn("<h1>오늘 라이브스코어", t)
        self.assertIn("워싱턴 위저즈", t)
        self.assertIn("61 : 86", t)
        self.assertLess(t.index("워싱턴 위저즈"), t.index("올랜도 매직"))   # live first
        self.assertNotIn("예시 FC", t)                                       # samples skipped
        self.assertIn("진행 중 1개", t)
        self.assertIn("public, max-age=60", r.headers["cache-control"])

    def test_league_table_snapshot(self):
        from app import sportiq_team_pages as tp   # team links: tests/test_team_pages.py
        with patch.object(dc, "get_standings", return_value=EPL), patch.object(tp, "team_slug_lookup", return_value={}):
            r = CLIENT.get("/standings/epl")
        t = r.text
        self.assertEqual(r.status_code, 200)
        self.assertIn("<title>프리미어리그(EPL) 순위표", t)
        self.assertIn('href="https://billnojay.com/standings/epl"', t)
        self.assertIn("<td>Arsenal</td>", t)
        self.assertIn("Brighton &lt;b&gt;", t)                              # escaped
        self.assertIn("현재 1위: Arsenal", t)
        self.assertIn('href="/standings/nba"', t)

    def test_grouped_table_has_sections(self):
        with patch.object(dc, "get_standings", return_value=NBA):
            t = CLIENT.get("/standings/nba").text
        self.assertIn("<h2>동부</h2>", t)
        self.assertIn("<h2>서부</h2>", t)
        self.assertIn(".833", t)
        self.assertIn("동부 1위 Philadelphia 76ers", t)

    def test_sample_or_unavailable_table_shows_no_rows(self):
        for data in (SAMPLE, {"unavailable": True, "standings": [], "is_sample": False}):
            fresh()
            with patch.object(dc, "get_standings", return_value=data):
                t = CLIENT.get("/standings/epl").text
            self.assertNotIn("리버풀", t)
            self.assertIn("불러오지 못했습니다", t)

    def test_unknown_league_is_404_noindex(self):
        r = CLIENT.get("/standings/kbo")
        self.assertEqual(r.status_code, 404)
        self.assertIn(seo.NOINDEX, r.text)

    def test_standings_index(self):
        t = CLIENT.get("/standings").text
        for slug in seo.STANDINGS_LEAGUES:
            self.assertIn(f'href="/standings/{slug}"', t)


class SmallFixesTests(unittest.TestCase):
    def test_trailing_slash_redirect_keeps_https(self):
        r = CLIENT.get("/sitemap.xml/", headers={"x-forwarded-proto": "https", "host": "billnojay.com"}, follow_redirects=False)
        self.assertIn(r.status_code, (307, 308))
        self.assertEqual(r.headers["location"], "https://billnojay.com/sitemap.xml")

    def test_current_bundle_version_is_immutable(self):
        r = CLIENT.get(f"/api/member/p0p1-upgrade-bundle?v={lr._BOOT_VER_V30}")
        self.assertEqual(r.headers["cache-control"], "public, max-age=31536000, immutable")
        for q in ("?v=46640_1", ""):
            r = CLIENT.get(f"/api/member/p0p1-upgrade-bundle{q}")
            self.assertIn("no-store", r.headers["cache-control"])
        self.assertIn(f"p0p1-upgrade-bundle?v={lr._BOOT_VER_V30}", CLIENT.get("/").text)


if __name__ == "__main__":
    unittest.main()
