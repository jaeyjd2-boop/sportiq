"""IndexNow key file and daily submission (no network: urlopen mocked)."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import date, datetime
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_indexnow as ix  # noqa: E402
from app import sportiq_results_pages as rp  # noqa: E402
from app import sportiq_team_pages as tp  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)
KEY = "3d607ed829cf5ec71c53df02918ffde5"


class FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


DAY = {"day": "2026-11-24", "finished": 6, "leagues": [
    {"key": "EPL", "games": [{"id": "1", "home": {"id": "359", "name": "Arsenal"}, "away": {"id": "363", "name": "Chelsea"}, "hs": 2, "as": 1}]},
    {"key": "UCL", "games": [{"id": "2", "home": {"id": "359", "name": "Arsenal"}, "away": {"id": "86", "name": "Real Madrid"}, "hs": 1, "as": 1}]},
]}
TEAMS = [{"id": "359", "slug": "arsenal", "gp": 11}, {"id": "363", "slug": "chelsea", "gp": 2}]


class KeyFileTests(unittest.TestCase):
    def test_key_file_returns_exactly_the_key(self):
        r = CLIENT.get(f"/{KEY}.txt")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, KEY)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertEqual(ix.KEY, KEY)


class SubmitTests(unittest.TestCase):
    def test_payload_and_status(self):
        sent = []

        def fake_urlopen(req, timeout=0):
            sent.append((req.full_url, req.get_method(), json.loads(req.data.decode("utf-8"))))
            return FakeResp(202)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            st = ix.submit(["https://billnojay.com/a", "https://billnojay.com/a", "https://evil.example/x"])
        self.assertEqual(st, [202])
        url, method, body = sent[0]
        self.assertEqual((url, method), ("https://api.indexnow.org/indexnow", "POST"))
        self.assertEqual(body, {"host": "billnojay.com", "key": KEY,
                                "keyLocation": f"https://billnojay.com/{KEY}.txt",
                                "urlList": ["https://billnojay.com/a"]})

    def test_chunks_of_10000(self):
        with patch("urllib.request.urlopen", return_value=FakeResp(200)) as m:
            st = ix.submit([f"https://billnojay.com/p{i}" for i in range(10001)])
        self.assertEqual(st, [200, 200])
        self.assertEqual(m.call_count, 2)

    def test_failure_never_raises(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(ix.submit(["https://billnojay.com/a"]), [0])
        err = urllib.error.HTTPError(ix.ENDPOINT, 403, "Forbidden", {}, io.BytesIO(b""))
        with patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(ix.submit(["https://billnojay.com/a"]), [403])


class DailyJobTests(unittest.TestCase):
    def setUp(self):
        ix._ensure_table()
        with ix._conn() as c:
            c.execute(f"DELETE FROM {ix.TABLE}")
        self.p = [patch.object(rp, "day_results", return_value=DAY),
                  patch.object(tp, "league_teams", side_effect=lambda lg: TEAMS if lg == "epl" else [])]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_urls_for_day(self):
        urls = ix.urls_for_day(date(2026, 11, 24))
        self.assertEqual(urls, ["https://billnojay.com/livescore/2026-11-24", "https://billnojay.com/team/epl/arsenal"])

    def test_runs_once_a_day_after_8_kst(self):
        early = datetime(2026, 11, 25, 7, 30, tzinfo=ix.KST)
        later = datetime(2026, 11, 25, 9, 0, tzinfo=ix.KST)
        with patch.object(ix, "submit", return_value=[202]) as sub:
            self.assertIsNone(ix.run_daily(early))
            self.assertEqual(ix.run_daily(later), (2, [202]))
            self.assertIsNone(ix.run_daily(later))          # logged: not sent again
        self.assertEqual(sub.call_count, 1)
        self.assertTrue(ix.already_sent(date(2026, 11, 24)))

    def test_network_failure_is_retried_but_bad_key_is_not(self):
        later = datetime(2026, 11, 25, 9, 0, tzinfo=ix.KST)
        with patch.object(ix, "submit", return_value=[0]):
            ix.run_daily(later)
        self.assertFalse(ix.already_sent(date(2026, 11, 24)))
        with patch.object(ix, "submit", return_value=[403]):
            ix.run_daily(later)
        self.assertTrue(ix.already_sent(date(2026, 11, 24)))

    def test_job_errors_do_not_raise(self):
        with patch.object(rp, "day_results", side_effect=RuntimeError("boom")):
            self.assertIsNone(ix.run_daily(datetime(2026, 11, 25, 9, 0, tzinfo=ix.KST)))

    def test_log_table_in_pg_snapshot_list(self):
        from app import sportiq_auto_analyst as aa
        self.assertIn(ix.TABLE, aa._PG_SNAPSHOT_TABLES)

    def test_enabled_only_on_render_or_flag(self):
        with patch.dict(os.environ, {"INDEXNOW_ENABLED": "", "RENDER": ""}):
            self.assertFalse(ix.enabled())
        with patch.dict(os.environ, {"INDEXNOW_ENABLED": "", "RENDER": "true"}):
            self.assertTrue(ix.enabled())
        with patch.dict(os.environ, {"INDEXNOW_ENABLED": "0", "RENDER": "true"}):
            self.assertFalse(ix.enabled())


if __name__ == "__main__":
    unittest.main()
