"""경기판 (gyeonggipan.com) pages poll /v1/matches/live from the browser, so billnojay allows that origin."""
import os
import tempfile
import unittest

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

CLIENT = TestClient(app)


class CorsTests(unittest.TestCase):
    def preflight(self, origin):
        return CLIENT.options("/v1/matches/live?sport=football",
                              headers={"Origin": origin, "Access-Control-Request-Method": "GET"})

    def test_gyeonggipan_is_allowed(self):
        for origin in ("https://gyeonggipan.com", "https://www.gyeonggipan.com"):
            self.assertEqual(self.preflight(origin).headers.get("access-control-allow-origin"), origin)

    def test_other_sites_are_not(self):
        r = self.preflight("https://example.com")
        self.assertIsNone(r.headers.get("access-control-allow-origin"))


if __name__ == "__main__":
    unittest.main()
