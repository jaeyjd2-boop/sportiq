"""Result/status sync and head-to-head regression tests (no network, scratch SQLite only)."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, text

from app import collector
from app.match_status import is_status_stale, with_stale_flag
from app.providers import football_data
from app.providers.football_data import FootballDataClient

NOW = datetime.now(timezone.utc)

SCHEMA = [
    "CREATE TABLE sports(id INTEGER PRIMARY KEY, code TEXT UNIQUE)",
    "CREATE TABLE leagues(id INTEGER PRIMARY KEY, sport_id INT, provider_league_id TEXT, name TEXT)",
    "CREATE TABLE teams(id INTEGER PRIMARY KEY, sport_id INT, provider_team_id TEXT, name TEXT)",
    """CREATE TABLE matches(id INTEGER PRIMARY KEY, sport_id INT, league_id INT, provider_match_id TEXT,
       home_team_id INT, away_team_id INT, start_time TIMESTAMP, status TEXT, home_score REAL, away_score REAL)""",
]


def make_db(rows):
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for stmt in SCHEMA:
            c.execute(text(stmt))
        c.execute(text("INSERT INTO sports VALUES (1,'football')"))
        c.execute(text("INSERT INTO leagues VALUES (1,1,'fd:2003','Eredivisie'),(2,1,'fd:2015','Ligue 1'),(3,1,'1234','legacy')"))
        c.execute(text("INSERT INTO teams VALUES (1,1,'fd:1','A'),(2,1,'fd:2','B'),(3,1,'fd:3','C'),(4,1,'fd:4','D')"))
        for r in rows:
            c.execute(text("""INSERT INTO matches(id,sport_id,league_id,provider_match_id,home_team_id,away_team_id,start_time,status)
                              VALUES (:id,1,:lid,:pid,:h,:a,:start,:status)"""), r)
    return engine


class FakeClient:
    instances = []

    def __init__(self):
        self.calls = []
        self.h2h = {}
        FakeClient.instances.append(self)

    def competition_fixtures_range(self, code, start, end):
        self.calls.append((code, start, end))
        return [{"fixture": {"id": "fd:x"}}]

    def head_to_head(self, pid, limit):
        self.calls.append(("h2h", pid))
        return [
            {"fixture": {"status": {"short": "FT"}}, "goals": {"home": 1, "away": 0}},
            {"fixture": {"status": {"short": "NS"}}, "goals": {"home": None, "away": None}},
        ]

    def close(self):
        pass


class RecheckUnfinishedTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []

    def test_stale_nonfinal_matches_are_refetched_per_competition(self):
        engine = make_db([
            # Stuck LIVE from two days ago (Eredivisie) and stuck NS yesterday (Ligue 1).
            {"id": 1, "lid": 1, "pid": "fd:10", "h": 1, "a": 2, "start": NOW - timedelta(days=2), "status": "LIVE"},
            {"id": 2, "lid": 2, "pid": "fd:11", "h": 3, "a": 4, "start": NOW - timedelta(hours=6), "status": "NS"},
            # Not eligible: already final, too recent, legacy provider, older than window.
            {"id": 3, "lid": 1, "pid": "fd:12", "h": 1, "a": 3, "start": NOW - timedelta(hours=6), "status": "FT"},
            {"id": 4, "lid": 1, "pid": "fd:13", "h": 2, "a": 4, "start": NOW - timedelta(minutes=30), "status": "LIVE"},
            {"id": 5, "lid": 3, "pid": "999", "h": 1, "a": 4, "start": NOW - timedelta(hours=6), "status": "NS"},
            {"id": 6, "lid": 1, "pid": "fd:14", "h": 3, "a": 2, "start": NOW - timedelta(days=5), "status": "NS"},
        ])
        with patch.object(collector, "engine", engine), patch.object(collector, "FootballDataClient", FakeClient), \
                patch.object(collector, "store_fixtures", side_effect=lambda items: len(items)) as store:
            self.assertEqual(collector.recheck_unfinished(), 2)
        calls = FakeClient.instances[0].calls
        self.assertEqual({c[0] for c in calls}, {"2003", "2015"})
        day = (NOW - timedelta(days=2)).date().isoformat()
        self.assertIn(("2003", day, day), calls)
        self.assertEqual(store.call_count, 2)

    def test_nothing_stale_makes_no_api_call(self):
        engine = make_db([{"id": 1, "lid": 1, "pid": "fd:10", "h": 1, "a": 2,
                           "start": NOW - timedelta(hours=6), "status": "FT"}])
        with patch.object(collector, "engine", engine), patch.object(collector, "FootballDataClient", FakeClient):
            self.assertEqual(collector.recheck_unfinished(), 0)
        self.assertEqual(FakeClient.instances, [])

    def test_invalid_arguments(self):
        for kwargs in ({"min_hours_after_kickoff": 0}, {"max_days_back": 8}):
            with self.assertRaises(ValueError):
                collector.recheck_unfinished(**kwargs)


class HeadToHeadTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []

    def test_fetches_only_pairs_without_stored_meeting_and_keeps_finished(self):
        engine = make_db([
            {"id": 1, "lid": 1, "pid": "fd:20", "h": 1, "a": 2, "start": NOW + timedelta(days=1), "status": "NS"},
            {"id": 2, "lid": 1, "pid": "fd:21", "h": 3, "a": 4, "start": NOW + timedelta(days=1), "status": "NS"},
            # 4 vs 3 already met (reverse fixture) -> match 2 is skipped.
            {"id": 3, "lid": 1, "pid": "fd:22", "h": 4, "a": 3, "start": NOW - timedelta(days=20), "status": "FT"},
            # Outside the 3-day window.
            {"id": 4, "lid": 1, "pid": "fd:23", "h": 1, "a": 3, "start": NOW + timedelta(days=9), "status": "NS"},
        ])
        stored = []
        with patch.object(collector, "engine", engine), patch.object(collector, "FootballDataClient", FakeClient), \
                patch.object(collector, "store_fixtures", side_effect=lambda items: stored.append(items) or len(items)):
            self.assertEqual(collector.collect_head_to_head(), 1)
        self.assertEqual(FakeClient.instances[0].calls, [("h2h", "fd:20")])
        self.assertEqual(len(stored[0]), 1)
        self.assertEqual(stored[0][0]["fixture"]["status"]["short"], "FT")

    def test_plan_without_head2head_stops(self):
        engine = make_db([{"id": 1, "lid": 1, "pid": "fd:20", "h": 1, "a": 2,
                           "start": NOW + timedelta(days=1), "status": "NS"}])

        class Forbidden(FakeClient):
            def head_to_head(self, pid, limit):
                raise httpx.HTTPStatusError("403", request=httpx.Request("GET", "http://x"),
                                            response=httpx.Response(403))
        with patch.object(collector, "engine", engine), patch.object(collector, "FootballDataClient", Forbidden):
            with self.assertRaises(httpx.HTTPStatusError):
                collector.collect_head_to_head()


class ProviderTests(unittest.TestCase):
    def test_throttle_is_shared_between_client_instances(self):
        sleeps = []
        football_data._LAST_REQUEST[0] = 0.0
        with patch.object(football_data.time, "sleep", side_effect=sleeps.append):
            football_data._throttle()
            football_data._throttle()
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 7.0)

    def test_rate_limited_competition_is_retried_once(self):
        client = object.__new__(FootballDataClient)
        attempts = {}

        def fake_get(path, params=None):
            if path == "/competitions":
                return {"competitions": [{"code": "DED"}, {"code": "FL1"}]}
            code = path.split("/")[2]
            attempts[code] = attempts.get(code, 0) + 1
            if code == "DED" and attempts[code] == 1:
                raise httpx.HTTPStatusError("429", request=httpx.Request("GET", "http://x"),
                                            response=httpx.Response(429))
            return {"matches": [{"id": len(attempts) * 100 + attempts[code], "status": "FINISHED",
                                 "competition": {"id": 1}, "homeTeam": {"id": 1}, "awayTeam": {"id": 2},
                                 "score": {"fullTime": {"home": 1, "away": 1}}}]}
        client.get = fake_get
        results = client.fixtures_range("2026-11-21", "2026-11-22")
        self.assertEqual(attempts, {"DED": 2, "FL1": 1})
        self.assertEqual(len(results), 2)
        self.assertEqual(client.last_scan["failed"], 0)


class WorkerWindowTests(unittest.TestCase):
    def test_fixture_window_includes_yesterday(self):
        import app.worker as worker
        with patch.object(worker, "collect_fixtures", return_value=0) as cf:
            worker.fixture_window()
        start, _ = cf.call_args.args
        self.assertEqual(start, datetime.now(timezone.utc).date() - timedelta(days=1))


class StaleFlagTests(unittest.TestCase):
    def test_stale_flag(self):
        self.assertTrue(is_status_stale(NOW - timedelta(hours=5), "LIVE", NOW))
        self.assertTrue(is_status_stale((NOW - timedelta(days=1)).isoformat(), "NS", NOW))
        self.assertFalse(is_status_stale(NOW - timedelta(hours=2), "LIVE", NOW))
        self.assertFalse(is_status_stale(NOW - timedelta(days=1), "FT", NOW))
        self.assertFalse(is_status_stale(None, "LIVE", NOW))
        self.assertEqual(with_stale_flag({"start_time": NOW + timedelta(hours=1), "status": "NS"})["status_stale"], False)


if __name__ == "__main__":
    unittest.main()
