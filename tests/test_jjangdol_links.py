"""jjangdoltv league deep links (no network: _get mocked)."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_jjangdol_links as jl  # noqa: E402
from app import sportiq_seo as seo  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)
B = "https://jjangdoltv.com"


def sitemap(*paths):
    locs = "".join(f"<url><loc>{B}{p}</loc></url>" for p in paths)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'.encode()


def fake_get(pages):
    """pages: {url: (status, body)}; anything else answers 404."""
    def _g(url, timeout=15.0):
        return pages.get(url, (404, b""))
    return _g


class CheckTests(unittest.TestCase):
    def test_listed_and_200_becomes_deep_link(self):
        pages = {B + "/sitemap.xml": (200, sitemap("/", "/sports", "/sports/epl", "/sports/nba")),
                 B + "/sports/epl": (200, b"ok"), B + "/sports/nba": (404, b"")}
        with patch.object(jl, "_get", side_effect=fake_get(pages)):
            out = jl.check({}, ["epl", "nba", "mlb"])
        self.assertEqual(out, {"epl": B + "/sports/epl"})

    def test_unlisted_page_is_not_probed(self):
        seen = []
        pages = {B + "/sitemap.xml": (200, sitemap("/", "/sports"))}

        def g(url, timeout=15.0):
            seen.append(url)
            return fake_get(pages)(url)
        with patch.object(jl, "_get", side_effect=g):
            self.assertEqual(jl.check({}, ["epl", "mlb"]), {})
        self.assertEqual(seen, [B + "/sitemap.xml"])

    def test_unreadable_sitemap_keeps_previous(self):
        with patch.object(jl, "_get", return_value=(0, b"")):
            self.assertIsNone(jl.check({"epl": B + "/sports/epl"}, ["epl"]))

    def test_network_error_on_page_keeps_old_value_but_404_drops_it(self):
        pages = {B + "/sitemap.xml": (200, sitemap("/sports/epl", "/sports/mlb")),
                 B + "/sports/epl": (0, b""), B + "/sports/mlb": (404, b"")}
        prev = {"epl": B + "/sports/epl", "mlb": B + "/sports/mlb"}
        with patch.object(jl, "_get", side_effect=fake_get(pages)):
            self.assertEqual(jl.check(prev, ["epl", "mlb"]), {"epl": B + "/sports/epl"})

    def test_sitemap_index_is_followed(self):
        index = f'<sitemapindex><sitemap><loc>{B}/sitemap-pages.xml</loc></sitemap></sitemapindex>'.encode()
        pages = {B + "/sitemap.xml": (200, index), B + "/sitemap-pages.xml": (200, sitemap("/sports/mlb/")),
                 B + "/sports/mlb": (200, b"ok")}
        with patch.object(jl, "_get", side_effect=fake_get(pages)):
            self.assertEqual(jl.check({}, ["mlb"]), {"mlb": B + "/sports/mlb"})

    def test_slugs_match_standings_leagues(self):
        self.assertEqual(jl.league_slugs(), list(seo.STANDINGS_LEAGUES))


class StateAndApiTests(unittest.TestCase):
    def tearDown(self):
        jl._save({}, 0.0)

    def test_url_for_falls_back_to_home(self):
        jl._save({"epl": B + "/sports/epl"}, 1.0)
        self.assertEqual(jl.url_for("epl"), B + "/sports/epl")
        self.assertEqual(jl.url_for("nba"), B + "/")
        self.assertEqual(jl.url_for(None), B + "/")

    def test_failed_run_keeps_stored_links(self):
        jl._save({"epl": B + "/sports/epl"}, 1.0)
        with patch.object(jl, "_get", return_value=(503, b"")):
            self.assertIsNone(jl.run_check())
        self.assertEqual(jl.current(), {"epl": B + "/sports/epl"})

    def test_api(self):
        jl._save({"nba": B + "/sports/nba"}, 1700000000.0)
        r = CLIENT.get("/api/jjangdol/links")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["root"], B + "/")
        self.assertEqual(d["rel"], "nofollow sponsored noopener")
        self.assertEqual(d["leagues"], {"nba": B + "/sports/nba"})

    def test_standings_note_is_nofollow(self):
        jl._save({"epl": B + "/sports/epl"}, 1.0)
        html = seo._stream_note("epl", "프리미어리그(EPL)")
        self.assertIn(f'href="{B}/sports/epl"', html)
        self.assertIn('rel="nofollow sponsored noopener"', html)
        self.assertIn(f'href="{B}/"', seo._stream_note("nba", "NBA"))


class ModalLinkTests(unittest.TestCase):
    def test_modal_link_is_nofollow(self):
        from app import sportiq_member_upgrade as mu
        js = mu.IQ_DUAL_DOCK_AND_MOBILE_V15_JS
        self.assertIn("rel='nofollow sponsored noopener'", js)
        self.assertNotIn("rel='noopener noreferrer' class='sq-jjangdol-cta'", js)
        self.assertIn("/api/jjangdol/links", js)


if __name__ == "__main__":
    unittest.main()
