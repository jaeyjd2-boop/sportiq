"""Live poll: 1 request per run, dropped live matches closed out, 120 s floor.

football-data is faked with httpx.MockTransport; no network, no token used.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, text

from app import collector
from app.providers import football_data

NOW = datetime.now(timezone.utc)
TODAY = NOW.date().isoformat()


def fd_match(mid, status, comp=2021, home=1, away=2, score=(None, None), when=None):
    return {"id": mid, "utcDate": (when or NOW).strftime("%Y-%m-%dT%H:%M:%SZ"), "status": status,
            "competition": {"id": comp, "name": "PL"}, "area": {"name": "England"},
            "homeTeam": {"id": home, "name": f"T{home}"}, "awayTeam": {"id": away, "name": f"T{away}"},
            "score": {"fullTime": {"home": score[0], "away": score[1]},
                      "regularTime": {"home": score[0], "away": score[1]}}}


class FakeFootballData:
    """Records requests; serves /matches?status=LIVE and competition ranges."""

    def __init__(self, live, competitions=None):
        self.live = live
        self.competitions = competitions or {}
        self.requests = []

    def handler(self, request):
        self.requests.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/v4/matches" and request.url.params.get("status") == "LIVE":
            return httpx.Response(200, json={"matches": self.live})
        if request.url.path.startswith("/v4/competitions/"):
            code = request.url.path.split("/")[3]
            return httpx.Response(200, json={"matches": self.competitions.get(code, [])})
        return httpx.Response(404, json={})


def make_db(rows):
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE sports(id INTEGER PRIMARY KEY, code TEXT)"))
        c.execute(text("CREATE TABLE leagues(id INTEGER PRIMARY KEY, provider_league_id TEXT)"))
        c.execute(text("""CREATE TABLE matches(id INTEGER PRIMARY KEY, sport_id INT, league_id INT,
                          provider_match_id TEXT, start_time TIMESTAMP, status TEXT, home_score INT, away_score INT)"""))
        c.execute(text("INSERT INTO sports VALUES (1,'football')"))
        c.execute(text("INSERT INTO leagues VALUES (1,'fd:2021'),(2,'fd:2015')"))
        for r in rows:
            c.execute(text("INSERT INTO matches(sport_id,league_id,provider_match_id,start_time,status) "
                           "VALUES (1,:lid,:pid,:start,:status)"), r)
    return engine


class LivePollTests(unittest.TestCase):
    def run_live(self, fake, engine, **kw):
        stored = []

        def fake_store(items):
            # Mirror store_fixtures' effect on status/score using the provider values.
            with engine.begin() as c:
                for x in items:
                    c.execute(text("UPDATE matches SET status=:st, home_score=:h, away_score=:a WHERE provider_match_id=:pid"),
                              {"st": x["fixture"]["status"]["short"], "h": x["goals"]["home"],
                               "a": x["goals"]["away"], "pid": x["fixture"]["id"]})
            stored.append(items)
            return len(items)

        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(fake.handler)
            return real_client(*args, **kwargs)

        with patch.object(football_data.settings, "football_data_token", "test-token"), \
                patch.object(football_data.httpx, "Client", side_effect=client_factory), \
                patch.object(football_data.time, "sleep"), \
                patch.object(collector, "engine", engine), \
                patch.object(collector, "store_fixtures", side_effect=fake_store):
            result = collector.collect_live(**kw)
        return result, stored

    def status(self, engine, pid):
        with engine.connect() as c:
            return c.execute(text("SELECT status, home_score, away_score FROM matches WHERE provider_match_id=:p"),
                             {"p": pid}).one()

    def test_no_live_matches_costs_one_request(self):
        engine = make_db([])
        fake = FakeFootballData(live=[])
        result, stored = self.run_live(fake, engine)
        self.assertEqual(result, 0)
        self.assertEqual(stored, [])
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0], ("/v4/matches", {"status": "LIVE"}))

    def test_live_feed_is_stored_without_catalog_rescan(self):
        engine = make_db([{"lid": 1, "pid": "fd:10", "start": NOW - timedelta(minutes=40), "status": "NS"}])
        fake = FakeFootballData(live=[fd_match(10, "IN_PLAY", score=(1, 0))])
        result, stored = self.run_live(fake, engine)
        self.assertEqual(result, 1)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(tuple(self.status(engine, "fd:10")), ("LIVE", 1, 0))
        self.assertFalse(any(p == "/v4/competitions" for p, _ in fake.requests))

    def test_match_that_left_the_live_feed_gets_its_final_status(self):
        engine = make_db([
            {"lid": 1, "pid": "fd:10", "start": NOW - timedelta(minutes=40), "status": "LIVE"},   # still live
            {"lid": 1, "pid": "fd:11", "start": NOW - timedelta(hours=2), "status": "2H"},        # just finished
            {"lid": 1, "pid": "fd:12", "start": NOW - timedelta(days=2), "status": "LIVE"},       # too old: recheck_unfinished's job
        ])
        fake = FakeFootballData(
            live=[fd_match(10, "IN_PLAY", score=(0, 0))],
            competitions={"2021": [fd_match(11, "FINISHED", score=(2, 3), when=NOW - timedelta(hours=2))]},
        )
        result, _ = self.run_live(fake, engine)
        self.assertEqual(len(fake.requests), 2)
        path, params = fake.requests[1]
        self.assertEqual(path, "/v4/competitions/2021/matches")
        day = (NOW - timedelta(hours=2)).date().isoformat()
        self.assertEqual(params, {"dateFrom": day, "dateTo": day})
        self.assertEqual(tuple(self.status(engine, "fd:11")), ("FT", 2, 3))
        self.assertEqual(self.status(engine, "fd:12")[0], "LIVE")
        self.assertEqual(result, 2)

    def test_provider_status_is_kept_as_is(self):
        # If the provider still says IN_PLAY for the competition/day, we store that, not a guess.
        engine = make_db([{"lid": 2, "pid": "fd:20", "start": NOW - timedelta(hours=3), "status": "LIVE"}])
        fake = FakeFootballData(live=[], competitions={"2015": [fd_match(20, "IN_PLAY", comp=2015, score=(1, 1))]})
        self.run_live(fake, engine)
        self.assertEqual(self.status(engine, "fd:20")[0], "LIVE")

    def test_rechecks_are_capped_per_run(self):
        rows = [{"lid": 1, "pid": f"fd:{30 + i}", "start": NOW - timedelta(hours=1, days=0) - timedelta(minutes=i), "status": "LIVE"}
                for i in range(2)]
        rows += [{"lid": 2, "pid": "fd:40", "start": NOW - timedelta(hours=1), "status": "HT"}]
        engine = make_db(rows)
        fake = FakeFootballData(live=[])
        self.run_live(fake, engine, max_rechecks=1)
        self.assertEqual(len(fake.requests), 2)

    def test_live_items_without_team_ids_are_skipped(self):
        bad = fd_match(50, "IN_PLAY")
        bad["homeTeam"] = {"id": None}
        engine = make_db([])
        fake = FakeFootballData(live=[bad])
        result, stored = self.run_live(fake, engine)
        self.assertEqual((result, stored), (0, []))


class LiveIntervalTests(unittest.TestCase):
    def test_interval_uses_setting_with_120s_floor(self):
        from app import worker
        self.assertEqual(worker.live_poll_interval_seconds(600), 600)
        self.assertEqual(worker.live_poll_interval_seconds(60), 120)
        self.assertEqual(worker.live_poll_interval_seconds(3600), 3600)
        self.assertEqual(worker.live_poll_interval_seconds("bad"), 600)

    def test_live_job_scheduled_soon_after_start(self):
        from app import worker
        job = worker.scheduler.get_job("live")
        self.assertIsNotNone(job)
        self.assertEqual(job.trigger.interval.total_seconds(), worker.live_poll_interval_seconds())
        # Not a full interval after start: about one minute after the worker module loads.
        self.assertLessEqual(job.next_run_time, datetime.now(timezone.utc) + timedelta(minutes=1, seconds=5))


if __name__ == "__main__":
    unittest.main()
