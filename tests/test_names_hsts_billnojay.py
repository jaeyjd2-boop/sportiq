"""Korean team names on match cards, sitemap scope, HSTS (2026-11-25 review).

Scratch SQLite only; no network.
"""
import os
import re
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from sitemap_util import all_locs  # noqa: E402

from app import sportiq_lounge_router as lr  # noqa: E402
from app import sportiq_seo as seo  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)
HANGUL = re.compile("[가-힣]")


class TeamNameTests(unittest.TestCase):
    def test_full_name_used_when_short_name_misses(self):
        side = {"team": {"shortDisplayName": "Sanfreece", "displayName": "Sanfrecce Hiroshima"}}
        raw = lr._espn_side_name_v4666(side, "soccer")
        self.assertEqual(raw, "Sanfrecce Hiroshima")
        self.assertEqual(lr._localize_team_ko_v37(raw, "soccer"), "산프레체 히로시마")
        side = {"team": {"shortDisplayName": "V-Varen", "displayName": "V-Varen Nagasaki"}}
        self.assertEqual(lr._localize_team_ko_v37(lr._espn_side_name_v4666(side, "soccer"), "soccer"), "V-파렌 나가사키")

    def test_short_name_kept_when_it_already_hits(self):
        side = {"team": {"shortDisplayName": "Arsenal", "displayName": "Arsenal FC"}}
        self.assertEqual(lr._espn_side_name_v4666(side, "soccer"), "Arsenal")

    def test_no_source_stays_english(self):
        side = {"team": {"shortDisplayName": "Samaleswari SC", "displayName": "Samaleswari Sports Club"}}
        raw = lr._espn_side_name_v4666(side, "soccer")
        self.assertEqual(raw, "Samaleswari SC")                    # nothing invented
        self.assertEqual(lr._localize_team_ko_v37(raw, "soccer"), "Samaleswari SC")
        self.assertEqual(lr._espn_side_name_v4666({"athlete": {"shortName": "M. Carle"}}, "tennis"), "M. Carle")

    def test_front_end_table_fills_server_gaps(self):
        extra = lr._ko_extra_team_map_v4666()
        self.assertGreater(len(extra), 50)
        self.assertEqual(lr._localize_team_ko_v37("Lions", "american_football"), "디트로이트 라이온스")
        self.assertEqual(lr._localize_team_ko_v37("Besiktas", "soccer"), "베식타스")
        # Exact keys and soccer/NFL only: an NBA "Orlando" is not turned into Orlando City.
        self.assertEqual(lr._localize_team_ko_v37("Orlando", "basketball"), "Orlando")
        for v in extra.values():
            self.assertRegex(v, HANGUL)

    def test_espn_supplement_rows_are_localized(self):
        row = {"id": "NFL-1", "sport": "american_football", "league": "NFL", "home": "Lions", "away": "Bears",
               "state": "pre", "match_dt_kst": "11.27 (목) 02:30"}
        from app import sportiq_datacenter as dc
        with patch.object(dc, "_supplement_espn_matches", side_effect=lambda items, off: list(items) + [row]):
            out = lr._with_espn_supplement_v50([], 1)
        self.assertEqual((out[0]["home"], out[0]["away"]), ("디트로이트 라이온스", "시카고 베어스"))
        self.assertEqual(row["home"], "Lions")                      # source row not mutated


class SitemapScopeTests(unittest.TestCase):
    def test_sitemap_lists_only_real_public_pages(self):
        from app import sportiq_team_pages as tp   # team URLs: tests/test_team_pages.py
        with patch.object(tp, "sitemap_team_urls", return_value=[]):
            locs = all_locs(CLIENT)
        self.assertEqual(len(locs), len(set(locs)))
        self.assertEqual(set(locs), {seo.SITE + "/"} | {seo.SITE + p for p, _f, _pr in seo.PUBLIC_PATHS})
        for loc in locs:
            path = loc[len(seo.SITE):] or "/"
            r = CLIENT.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertNotIn("noindex", r.headers.get("x-robots-tag", ""), path)


class HstsTests(unittest.TestCase):
    def test_https_responses_send_hsts(self):
        for path in ("/", "/livescore", "/sitemap.xml", "/api/datacenter/standings?league=XX", "/favicon.svg"):
            r = CLIENT.get(path, headers={"x-forwarded-proto": "https"})
            self.assertEqual(r.headers.get("strict-transport-security"), "max-age=31536000", path)

    def test_plain_http_has_no_hsts(self):
        r = CLIENT.get("/favicon.svg", headers={"x-forwarded-proto": "http"})
        self.assertIsNone(r.headers.get("strict-transport-security"))
        self.assertIsNone(CLIENT.get("/favicon.svg").headers.get("strict-transport-security"))

    def test_redirects_keep_https_and_hsts(self):
        r = CLIENT.get("/sitemap.xml/", headers={"x-forwarded-proto": "https", "host": "billnojay.com"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "https://billnojay.com/sitemap.xml")
        self.assertEqual(r.headers.get("strict-transport-security"), "max-age=31536000")


if __name__ == "__main__":
    unittest.main()
