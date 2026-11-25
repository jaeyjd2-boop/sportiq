"""Server-rendered links in the home page's initial HTML."""
import os
import re
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_results_pages as rp  # noqa: E402
from app import sportiq_seo as seo  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)


class HomeLinksTests(unittest.TestCase):
    def test_home_html_has_crawlable_links(self):
        with patch.object(rp, "today_kst", return_value=date(2026, 11, 25)):
            t = CLIENT.get("/").text
        body = t[t.find("<body"):]
        hrefs = re.findall(r'<a\b[^>]*href="([^"]+)"', body)
        self.assertIn("/livescore", hrefs)
        self.assertIn("/standings", hrefs)
        for slug in seo.STANDINGS_LEAGUES:
            self.assertIn(f"/standings/{slug}", hrefs)
        self.assertIn("/livescore/2026-11-24", hrefs)
        self.assertIn("/livescore/2026-11-18", hrefs)
        self.assertNotIn("/livescore/2026-11-17", hrefs)
        self.assertGreaterEqual(len(hrefs), 20)
        self.assertEqual(t.count(f'id="{seo.HOME_LINKS_ID}"'), 1)

    def test_block_is_visible_and_inside_body(self):
        t = CLIENT.get("/").text
        i = t.find(f'id="{seo.HOME_LINKS_ID}"')
        self.assertGreater(i, t.find("<body"))
        self.assertLess(i, t.rfind("</body>"))
        block = t[t.rfind("<style>", 0, i):i]
        self.assertNotIn("display:none", block)
        self.assertNotIn("visibility:hidden", block)

    def test_inject_is_idempotent(self):
        html = "<html><body><p>x</p></body></html>"
        once = seo.inject_home_links(html)
        self.assertEqual(seo.inject_home_links(once), once)
        self.assertEqual(seo.inject_home_links("no body"), "no body")


if __name__ == "__main__":
    unittest.main()
