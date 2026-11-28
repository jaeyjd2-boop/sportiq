"""/privacy page, its footer links, and the home page without odds/winnings calculators."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_quick_picks as qp  # noqa: E402
from app import sportiq_seo  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)
ROOT = os.path.join(os.path.dirname(__file__), "..", "app")


class PrivacyPageTests(unittest.TestCase):
    def test_page_is_indexable_and_complete(self):
        r = CLIENT.get("/privacy")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn('<link rel="canonical" href="https://billnojay.com/privacy" />', html)
        self.assertIn('content="index, follow', html)
        for part in ("처리하는 개인정보", "보관 기간과 파기", "처리 위탁과 국외 이전", "Render", "Google Analytics",
                     "이용자의 권리", "개인정보 보호책임자", "시행일"):
            self.assertIn(part, html)
        self.assertNotIn("AdSense", html)

    def test_contact_comes_from_env_and_is_never_invented(self):
        with patch.dict(os.environ, {"PRIVACY_CONTACT_EMAIL": ""}):
            self.assertIn("운영자가 확정하는 대로", CLIENT.get("/privacy").text)
        with patch.dict(os.environ, {"PRIVACY_CONTACT_EMAIL": "privacy@example.org"}):
            self.assertIn('href="mailto:privacy@example.org"', CLIENT.get("/privacy").text)

    def test_in_pages_sitemap(self):
        self.assertIn("<loc>https://billnojay.com/privacy</loc>", CLIENT.get("/sitemap-pages.xml").text)

    def test_footers_link_it(self):
        self.assertIn('<a href="/privacy">개인정보처리방침</a>', sportiq_seo._page("/livescore", "t", "d", "h", "<p>x</p>"))
        self.assertIn('href="/privacy"', sportiq_seo.home_links_html())
        with patch.object(qp, "list_recent_picks", return_value=[]), \
             patch("app.sportiq_sister_links.upcoming_match_cards", return_value=[]):
            self.assertIn('<a href="/privacy">개인정보처리방침</a>', CLIENT.get("/picks").text)


class OddsToolsRemovedTests(unittest.TestCase):
    def test_home_template_has_no_odds_or_winnings_tools(self):
        html = open(os.path.join(ROOT, "sportiq_community.html"), encoding="utf-8").read()
        for gone in ("kellyValueList", "켈리(Kelly)", "No-Vig", "멀티폴더 합성배당", "적중금", "calcNoVig", "calcMultiParlay",
                     "loadValueOdds", "/api/autonomous/value-odds"):
            self.assertNotIn(gone, html)
        self.assertIn("calcBaseballWE", html)   # the baseball win-probability calculator stays


if __name__ == "__main__":
    unittest.main()
