"""Backfill of past finished matches for ESPN sports, and /analysis on top of it.

Scratch SQLite + fake ESPN (httpx.MockTransport); no network.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import collector, details, multisport, multisport_backfill as bf
from app.providers import api_sports, espn
from tests.test_multisport import FIXTURE_FOR, load, make_engine

NOW = datetime(2026, 11, 23, 3, tzinfo=timezone.utc)
TEAMS = ["1", "2"]


def sched_event(eid, when, home, away, hs, as_, state="post", name="STATUS_FINAL"):
    return {"id": str(eid), "date": when, "competitions": [{
        "id": str(eid), "date": when,
        "status": {"type": {"name": name, "state": state, "completed": state == "post", "shortDetail": "Final"}},
        "competitors": [
            {"homeAway": "home", "team": {"id": home, "displayName": f"Team {home}"}, "score": {"value": hs, "displayValue": str(hs)}},
            {"homeAway": "away", "team": {"id": away, "displayName": f"Team {away}"}, "score": {"value": as_, "displayValue": str(as_)}},
        ]}]}


def team_schedule(team_id, season=None):
    if season:
        # Previous season: eight older meetings of teams 1 and 2.
        evs = [sched_event(800 + i, (NOW - timedelta(days=400 - 10 * i)).strftime("%Y-%m-%dT%H:%MZ"),
                           "2" if i % 2 else "1", "1" if i % 2 else "2", 90 + i, 95) for i in range(8)]
        return {"events": evs}
    # Current season: six finished meetings of teams 1 and 2 plus one future game.
    evs = [sched_event(900 + i, (NOW - timedelta(days=60 - 7 * i)).strftime("%Y-%m-%dT%H:%MZ"),
                       "1" if i % 2 else "2", "2" if i % 2 else "1", 100 + i, 98) for i in range(6)]
    evs.append(sched_event(999, (NOW + timedelta(days=3)).strftime("%Y-%m-%dT%H:%MZ"), "1", "2", 0, 0, "pre", "STATUS_SCHEDULED"))
    return {"events": evs}


class FakeEspn:
    def __init__(self, fail=()):
        self.requests, self.fail = [], set(fail)

    def handler(self, request):
        path = request.url.path.removeprefix("/apis/site/v2/sports/")
        self.requests.append((path, dict(request.url.params)))
        lg = next((x for x in espn.LEAGUES if path.startswith(x.slug + "/")), None)
        if lg is None or lg.key in self.fail:
            return httpx.Response(500, json={})
        rest = path[len(lg.slug) + 1:]
        if rest == "teams":
            return httpx.Response(200, json={"sports": [{"leagues": [{"teams": [{"team": {"id": t}} for t in TEAMS]}]}]})
        if rest.startswith("teams/") and rest.endswith("/schedule"):
            season = dict(request.url.params).get("season")
            if lg.key == "MLB" and season == "2026":
                season = None          # MLB's "current" (just finished) season in the fake
            return httpx.Response(200, json=team_schedule(rest.split("/")[1], season))
        return httpx.Response(200, json=load(FIXTURE_FOR[lg.key]))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        self.patches = [patch.object(collector, "engine", self.eng), patch.object(multisport, "engine", self.eng),
                        patch.object(bf, "engine", self.eng), patch.object(details, "engine", self.eng),
                        patch.object(api_sports.settings, "api_sports_key", ""), patch.object(espn.time, "sleep")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def run_bf(self, fake, now=NOW, max_requests=25):
        client = espn.EspnClient(transport=httpx.MockTransport(fake.handler))
        return bf.collect_backfill(espn_client=client, now=now, max_requests=max_requests)

    def statuses(self):
        with self.eng.connect() as c:
            return {r[0] for r in c.execute(text("SELECT DISTINCT status FROM matches")).all()}

    def test_backfill_in_capped_runs_then_nothing_then_one_day(self):
        fake = FakeEspn()
        runs = []
        for _ in range(20):
            before = len(fake.requests)
            res = self.run_bf(fake)
            runs.append(len(fake.requests) - before)
            if res["progress"]["tasks_left"] == 0:
                break
        self.assertTrue(all(n <= 25 for n in runs), runs)
        self.assertGreater(len(runs), 1)
        # 5 date leagues x 29 days (2..30 days ago) + 3 event ranges + 4 team lists
        # + 4 leagues x 2 teams x 2 seasons (current, previous)
        self.assertEqual(sum(runs), 5 * 29 + 3 + 4 + 16)
        self.assertTrue(self.statuses() <= set(bf.FINISHED))     # finished matches only

        # Done tasks are never fetched again (e.g. after a redeploy).
        before = len(fake.requests)
        self.assertEqual(self.run_bf(fake)["progress"]["tasks_done"], 0)
        self.assertEqual(len(fake.requests), before)

        # Next day: one new day per league (date and event leagues).
        before = len(fake.requests)
        self.run_bf(fake, now=NOW + timedelta(days=1))
        self.assertEqual(len(fake.requests) - before, 5 + 3)

    def test_mlb_team_schedule_asks_for_finished_season(self):
        fake = FakeEspn()
        for _ in range(20):
            if self.run_bf(fake)["progress"]["tasks_left"] == 0:
                break
        def seasons(prefix):
            return sorted(p.get("season") or "default" for path, p in fake.requests
                          if path.startswith(prefix) and path.endswith("/schedule"))
        # Labels are start years (2026 = current); ESPN wants end years for NBA/NHL.
        self.assertEqual(seasons("baseball/mlb/teams/"), ["2025", "2025", "2026", "2026"])
        self.assertEqual(seasons("football/nfl/teams/"), ["2025", "2025", "default", "default"])
        self.assertEqual(seasons("basketball/nba/teams/"), ["2026", "2026", "default", "default"])
        self.assertEqual(seasons("hockey/nhl/teams/"), ["2026", "2026", "default", "default"])

    def test_season_param_format(self):
        self.assertEqual(bf.espn_season_param("NBA", "2025"), "2026")
        self.assertEqual(bf.espn_season_param("NHL", "2026"), "2027")
        self.assertEqual(bf.espn_season_param("NFL", "2025"), "2025")
        self.assertEqual(bf.espn_season_param("MLB", "2025"), "2025")

    def test_failed_request_is_retried_later(self):
        fake = FakeEspn(fail={"NBA"})
        self.run_bf(fake, max_requests=200)
        with self.eng.connect() as c:
            nba_done = c.execute(text("SELECT COUNT(*) FROM espn_backfill_progress WHERE task LIKE '%:NBA:%'")).scalar()
        self.assertEqual(nba_done, 0)
        fake.fail.clear()
        self.run_bf(fake, max_requests=200)
        with self.eng.connect() as c:
            nba_done = c.execute(text("SELECT COUNT(*) FROM espn_backfill_progress WHERE task LIKE '%:NBA:%'")).scalar()
        self.assertGreater(nba_done, 0)

    def test_analysis_gets_recent_form_and_head_to_head(self):
        fake = FakeEspn()
        for _ in range(20):
            if self.run_bf(fake)["progress"]["tasks_left"] == 0:
                break
        # Upcoming game from the regular schedule job.
        upcoming = espn.parse_scoreboard(team_schedule("1"), next(lg for lg in espn.LEAGUES if lg.key == "NBA"))
        multisport._store([x for x in upcoming if x["fixture"]["status"]["short"] == "NS"], "basketball", "espn:")
        with self.eng.connect() as c:
            mid = c.execute(text("SELECT id FROM matches WHERE provider_match_id='espn:999'")).scalar()
        app = FastAPI()
        app.include_router(details.router)
        an = TestClient(app).get(f"/v1/matches/{mid}/analysis").json()
        self.assertEqual(an["match"]["sport"], "basketball")
        self.assertEqual(len(an["home"]["recent"]), 5)
        self.assertEqual(len(an["away"]["recent"]), 5)
        # 6 meetings this season + 8 last season = 14; the existing cap keeps the 10 newest.
        h2h = an["head_to_head"]
        self.assertEqual(len(h2h), 10)
        dates = [str(x["start_time"])[:10] for x in h2h]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertTrue(any(d < (NOW - timedelta(days=300)).date().isoformat() for d in dates))  # last season included
        self.assertEqual(an["home"]["form"]["played"], 5)

    def test_football_analysis_unchanged(self):
        def fd(i, when, status="FT", hs=1, as_=0):
            return {"fixture": {"id": f"fd:{i}", "date": when, "status": {"short": status, "long": status},
                                "venue": {"name": None}, "referee": None},
                    "league": {"id": "fd:2021", "name": "PL"}, "country": {"name": "England"},
                    "teams": {"home": {"id": "fd:1", "name": "A"}, "away": {"id": "fd:2", "name": "B"}},
                    "goals": {"home": hs if status == "FT" else None, "away": as_ if status == "FT" else None}}
        collector.store_fixtures([fd(1, "2026-11-01T15:00:00Z"), fd(2, "2026-11-08T15:00:00Z"),
                                  fd(3, "2026-11-30T15:00:00Z", "NS")])
        with self.eng.connect() as c:
            mid = c.execute(text("SELECT id FROM matches WHERE provider_match_id='fd:3'")).scalar()
        app = FastAPI()
        app.include_router(details.router)
        an = TestClient(app).get(f"/v1/matches/{mid}/analysis").json()
        self.assertEqual(an["match"]["sport"], "football")
        self.assertEqual(an["match"]["source"], "football-data.org")
        self.assertEqual(len(an["head_to_head"]), 2)


class WorkerTests(unittest.TestCase):
    def test_backfill_job_registered(self):
        from app import worker
        job = worker.scheduler.get_job("multisport_backfill")
        self.assertEqual(job.trigger.interval, timedelta(minutes=10))


if __name__ == "__main__":
    unittest.main()
