"""Standings pages without a real table: noindex,follow and out of the sitemap."""
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
REAL = {"ok": True, "sport": "soccer", "is_sample": False, "source": "ESPN", "updated_at_kst": "x",
        "standings": [{"rank": 1, "team": "Arsenal", "gp": 11, "w": 9, "d": 1, "l": 1, "gd": 10, "_pts": 28}]}
UNAVAILABLE = {"ok": True, "sport": "soccer", "is_sample": False, "unavailable": True, "standings": []}
SAMPLE = {"ok": True, "sport": "soccer", "is_sample": True, "standings": [{"rank": 1, "team": "예시"}]}


def by_code(mapping):
    return lambda code: mapping.get(code, REAL)


class StandingsNoindexTests(unittest.TestCase):
    def setUp(self):
        seo._PAGE_CACHE.clear()
        self.tp = patch.object(tp, "team_slug_lookup", return_value={})
        self.tp.start()

    def tearDown(self):
        self.tp.stop()

    def get(self, slug, data):
        seo._PAGE_CACHE.clear()
        with patch.object(dc, "get_standings", side_effect=by_code({"KLEAGUE": data})):
            return CLIENT.get(f"/standings/{slug}")

    def test_no_data_page_is_noindex_follow(self):
        for data in (UNAVAILABLE, SAMPLE, {}):
            r = self.get("kleague", data)
            self.assertEqual(r.status_code, 200)
            self.assertIn('<meta name="robots" content="noindex, follow" />', r.text)
            self.assertEqual(r.headers.get("x-robots-tag"), seo.NOINDEX)
            self.assertNotIn("index, follow, max-snippet", r.text)

    def test_real_table_stays_indexable_and_recovers(self):
        r = self.get("kleague", REAL)
        self.assertIn("index, follow, max-snippet:-1", r.text)
        self.assertIsNone(r.headers.get("x-robots-tag"))
        self.assertIn("noindex", self.get("kleague", UNAVAILABLE).text)
        self.assertNotIn("noindex", self.get("kleague", REAL).text)       # data back -> indexable again

    def test_pages_sitemap_leaves_out_leagues_without_data(self):
        with patch.object(dc, "get_standings", side_effect=by_code({"KLEAGUE": UNAVAILABLE, "WNBA": SAMPLE})):
            locs = re.findall(r"<loc>(.*?)</loc>", CLIENT.get("/sitemap-pages.xml").text)
        self.assertNotIn("https://billnojay.com/standings/kleague", locs)
        self.assertNotIn("https://billnojay.com/standings/wnba", locs)
        self.assertIn("https://billnojay.com/standings/epl", locs)
        self.assertIn("https://billnojay.com/livescore", locs)
        self.assertIn("https://billnojay.com/standings", locs)

    def test_rss_leaves_out_leagues_without_data(self):
        with patch.object(dc, "get_standings", side_effect=by_code({"KLEAGUE": UNAVAILABLE})):
            rss = CLIENT.get("/rss.xml").text
        self.assertNotIn("/standings/kleague", rss)
        self.assertIn("/standings/epl", rss)

    def test_standings_has_data(self):
        with patch.object(dc, "get_standings", side_effect=by_code({"KLEAGUE": UNAVAILABLE})):
            self.assertFalse(seo.standings_has_data("kleague"))
            self.assertTrue(seo.standings_has_data("epl"))
            self.assertFalse(seo.standings_has_data("nope"))
        with patch.object(dc, "get_standings", side_effect=RuntimeError("down")):
            self.assertFalse(seo.standings_has_data("epl"))


if __name__ == "__main__":
    unittest.main()
