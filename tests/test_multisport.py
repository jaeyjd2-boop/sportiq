"""Multisport collector: ESPN parsing/storage, API-SPORTS adapter, API endpoints.

Scratch SQLite and fake HTTP (httpx.MockTransport) only: no network, no key.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text

from app import collector, multisport
from app.providers import api_sports, espn, kr_scoreboard

FIX = Path(__file__).parent / "fixtures" / "espn"
LEAGUE = {lg.key: lg for lg in espn.LEAGUES}
FIXTURE_FOR = {"NBA": "basketball_nba", "MLB": "baseball_mlb", "NHL": "hockey_nhl", "NFL": "football_nfl",
               "NCAAW_VB": "volleyball_ncaaw", "ATP": "tennis_atp", "WTA": "tennis_wta", "UFC": "mma_ufc"}
SHORT_CODES = {"NS", "LIVE", "HT", "FT", "AET", "PEN", "PST", "CANC", "SUSP", "ABD", "AWD"}


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


SCHEMA = [
    "CREATE TABLE sports(id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT)",
    """CREATE TABLE leagues(id INTEGER PRIMARY KEY, sport_id INT, provider_league_id TEXT, name TEXT, country TEXT,
       logo_url TEXT, coverage TEXT DEFAULT '{}', UNIQUE(sport_id, provider_league_id))""",
    """CREATE TABLE teams(id INTEGER PRIMARY KEY, sport_id INT, provider_team_id TEXT, name TEXT, country TEXT,
       logo_url TEXT, UNIQUE(sport_id, provider_team_id))""",
    """CREATE TABLE matches(id INTEGER PRIMARY KEY, sport_id INT, league_id INT, season_id INT, provider_match_id TEXT,
       home_team_id INT, away_team_id INT, start_time TIMESTAMP, status TEXT DEFAULT 'NS', status_detail TEXT,
       home_score REAL, away_score REAL, venue TEXT, referee TEXT, raw_payload TEXT, updated_at TIMESTAMP,
       UNIQUE(sport_id, provider_match_id))""",
    "CREATE TABLE match_team_stats(id INTEGER PRIMARY KEY, match_id INT, team_id INT, statistics TEXT, updated_at TEXT)",
    "CREATE TABLE match_events(id INTEGER PRIMARY KEY, match_id INT, event_type TEXT, event_time TEXT, player_name TEXT, detail TEXT)",
]


def make_engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)

    @event.listens_for(eng, "connect")
    def _now(conn, _rec):
        conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat(sep=" "))

    with eng.begin() as c:
        for s in SCHEMA:
            c.execute(text(s))
        c.execute(text("""INSERT INTO sports(code,name) VALUES ('football','축구'),('baseball','야구'),('basketball','농구'),
            ('volleyball','배구'),('tennis','테니스'),('hockey','하키'),('american_football','미식축구'),('mma','MMA')"""))
    return eng


class FakeEspn:
    def __init__(self, fail=()):
        self.requests, self.fail = [], set(fail)

    def handler(self, request):
        self.requests.append((request.url.path, dict(request.url.params)))
        slug = request.url.path.removeprefix("/apis/site/v2/sports/").removesuffix("/scoreboard")
        lg = next((x for x in espn.LEAGUES if x.slug == slug), None)
        if lg is None or lg.key in self.fail:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=load(FIXTURE_FOR[lg.key]))


def espn_client(fake):
    with patch.object(espn.time, "sleep"):
        return espn.EspnClient(transport=httpx.MockTransport(fake.handler))


class EspnParsingTests(unittest.TestCase):
    def test_every_league_fixture_parses(self):
        for key, name in FIXTURE_FOR.items():
            items = espn.parse_scoreboard(load(name), LEAGUE[key])
            self.assertTrue(items, key)
            for x in items:
                self.assertTrue(x["fixture"]["id"].startswith("espn:"), key)
                self.assertIn(x["fixture"]["status"]["short"], SHORT_CODES, key)
                self.assertEqual(x["league"]["id"], "espn:" + key)
                self.assertEqual(x["source"], "ESPN")
                self.assertNotEqual(x["teams"]["home"]["id"], x["teams"]["away"]["id"])

    def test_individual_sports_home_away_convention(self):
        atp = espn.parse_scoreboard(load("tennis_atp"), LEAGUE["ATP"])
        self.assertTrue(all(x["teams"]["home"]["id"].startswith(("espn:athlete:", "espn:pair:")) for x in atp))
        self.assertTrue(any(x["teams"]["home"]["id"].startswith("espn:pair:") for x in atp))  # doubles
        done = [x for x in atp if x["fixture"]["status"]["short"] == "FT"]
        self.assertTrue(all(isinstance(x["goals"]["home"], int) for x in done))  # sets won
        ufc = espn.parse_scoreboard(load("mma_ufc"), LEAGUE["UFC"])
        self.assertEqual(len(ufc), 3)
        self.assertTrue(all(x["goals"] == {"home": None, "away": None} for x in ufc))
        self.assertTrue(all(x["winner"] in ("home", "away", None) for x in ufc))

    def test_bad_shapes_give_empty(self):
        for bad in (None, [], {"events": None}, {"events": ["x", {"competitions": "bad"}]},
                    {"events": [{"id": 1, "competitions": [{"competitors": [{"team": {"id": 1}}]}]}]}):
            self.assertEqual(espn.parse_scoreboard(bad, LEAGUE["NBA"]), [])

    def test_status_mapping(self):
        self.assertEqual(espn.map_status({"type": {"name": "STATUS_FINAL_OT", "state": "post"}})[0], "AET")
        self.assertEqual(espn.map_status({"type": {"name": "STATUS_HALFTIME", "state": "in"}})[0], "HT")
        self.assertEqual(espn.map_status({"type": {"name": "STATUS_POSTPONED", "state": "post"}})[0], "PST")
        self.assertEqual(espn.map_status({"type": {"state": "in"}})[0], "LIVE")


class EspnClientTests(unittest.TestCase):
    def test_dates_league_one_request_per_us_date_and_failures_skip(self):
        fake = FakeEspn(fail={"MLB"})
        cl = espn_client(fake)
        first = datetime(2026, 11, 22, tzinfo=timezone.utc).date()
        with patch.object(espn.time, "sleep"):
            nba = cl.league_window(LEAGUE["NBA"], first, first + timedelta(days=1))
            mlb = cl.league_window(LEAGUE["MLB"], first, first)
        self.assertTrue(nba)
        self.assertEqual([p["dates"] for _, p in fake.requests[:3]], ["20261121", "20261122", "20261123"])
        self.assertEqual(mlb, [])

    def test_event_league_single_request_filtered_by_date(self):
        fake = FakeEspn()
        cl = espn_client(fake)
        with patch.object(espn.time, "sleep"):
            far = cl.league_window(LEAGUE["UFC"], datetime(2030, 1, 1).date(), datetime(2030, 1, 2).date())
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(far, [])


class CollectAndApiTests(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        self.patches = [patch.object(collector, "engine", self.eng), patch.object(multisport, "engine", self.eng),
                        patch.object(api_sports.settings, "api_sports_key", ""),
                        # no network: the Korean scoreboard has its own tests (KoreaScoreboardTests)
                        patch.object(multisport.settings, "kr_scoreboard_enabled", False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def run_schedule(self, now=None):
        fake = FakeEspn()
        with patch.object(espn.time, "sleep"):
            res = multisport.collect_schedule(espn_client=espn_client(fake), now=now)
        return res, fake

    def counts(self):
        with self.eng.connect() as c:
            return dict(c.execute(text("SELECT s.code, COUNT(*) FROM matches m JOIN sports s ON s.id=m.sport_id GROUP BY s.code")).all())

    def test_schedule_stores_each_sport_without_api_sports_requests(self):
        with patch.object(api_sports.ApiSportsClient, "__init__", side_effect=AssertionError("no key: must not build")),                 patch.object(multisport.settings, "fixture_lookahead_days", 2):
            res, fake = self.run_schedule(now=datetime(2026, 11, 22, 12, tzinfo=timezone.utc))
        counts = self.counts()
        for sport in ("basketball", "baseball", "hockey", "american_football", "volleyball", "tennis", "mma"):
            self.assertGreater(counts.get(sport, 0), 0, sport)
        self.assertNotIn("football", counts)
        # FIXTURE_LOOKAHEAD_DAYS=2 (render.yaml): yesterday..tomorrow = 3 UTC days, plus the
        # previous US date -> 4 requests per date-based league (5), 1 per event league (3).
        self.assertEqual(len(fake.requests), 5 * 4 + 3)
        self.assertFalse(any(k.startswith("as:") for k in res))

    def test_api_endpoints_for_new_sports(self):
        from app import details, main
        self.run_schedule(now=datetime(2026, 11, 22, 12, tzinfo=timezone.utc))
        app = FastAPI()
        app.include_router(details.router)
        app.get("/v1/matches")(main.matches)
        app.get("/v1/matches/{match_id}")(main.match_detail)
        with patch.object(main, "engine", self.eng), patch.object(details, "engine", self.eng):
            cl = TestClient(app)
            res = cl.get("/v1/matches", params={"sport": "basketball", "limit": 50}).json()
            self.assertGreater(res["total"], 0)
            self.assertTrue(all(x["source"] == "ESPN" and x["sport"] == "basketball" for x in res["items"]))
            mid = res["items"][0]["id"]
            self.assertEqual(cl.get(f"/v1/matches/{mid}").json()["source"], "ESPN")
            an = cl.get(f"/v1/matches/{mid}/analysis").json()
            self.assertEqual(an["match"]["sport"], "basketball")
            self.assertIn("head_to_head", an)
            fc = cl.get(f"/v1/matches/{mid}/forecast").json()
            self.assertFalse(fc["available"])
            ufc = cl.get("/v1/matches", params={"sport": "mma"}).json()
            self.assertGreater(ufc["total"], 0)

    def test_football_store_unchanged(self):
        item = {"fixture": {"id": "fd:1", "date": "2026-11-23T15:00:00Z", "status": {"short": "FT", "long": "FINISHED"},
                            "venue": {"name": None}, "referee": None},
                "league": {"id": "fd:2021", "name": "Premier League"}, "country": {"name": "England"},
                "teams": {"home": {"id": "fd:1", "name": "A"}, "away": {"id": "fd:2", "name": "B"}},
                "goals": {"home": 2, "away": 1}}
        self.assertEqual(collector.store_fixtures([item]), 1)
        self.assertEqual(self.counts(), {"football": 1})

    def test_live_poll_only_when_needed(self):
        fake = FakeEspn()
        with patch.object(espn.time, "sleep"):
            self.assertEqual(multisport.collect_live(espn_client=espn_client(fake)), {})
        self.assertEqual(fake.requests, [])


def as_game(gid, home, away, when, short="NS", hs=None, as_=None, league=12):
    return {"id": gid, "date": when, "status": {"short": short, "long": short},
            "league": {"id": league, "name": "NBA"}, "country": {"name": "USA"},
            "teams": {"home": {"id": gid * 10 + 1, "name": home}, "away": {"id": gid * 10 + 2, "name": away}},
            "scores": {"home": {"total": hs}, "away": {"total": as_}}}


class _ApiSportsBase(unittest.TestCase):
    """Scratch DB, fake API-SPORTS transport, key set, basketball fully on API-SPORTS."""

    def setUp(self):
        self.eng = make_engine()
        self.requests = []
        self.remaining = "90"
        self.games = []

        def handler(request):
            self.requests.append(str(request.url))
            return httpx.Response(200, json={"errors": [], "response": self.games},
                                  headers={"x-ratelimit-requests-remaining": self.remaining})
        self.transport = httpx.MockTransport(handler)
        self.patches = [patch.object(collector, "engine", self.eng), patch.object(multisport, "engine", self.eng),
                        patch.object(api_sports.settings, "api_sports_key", "test-key"),
                        patch.object(api_sports.settings, "api_sports_sports", "basketball"),
                        patch.object(api_sports.settings, "api_sports_daily_budget", 3),
                        patch.object(api_sports.time, "sleep")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def client(self):
        return api_sports.ApiSportsClient(self.eng, transport=self.transport)


class ApiSportsTests(_ApiSportsBase):
    def test_no_key_means_no_client_and_no_requests(self):
        with patch.object(api_sports.settings, "api_sports_key", ""):
            self.assertEqual(api_sports.active_sports(), set())
            self.assertEqual(multisport.source_for("basketball"), "espn")
            with self.assertRaises(RuntimeError):
                api_sports.ApiSportsClient(self.eng, transport=self.transport)
        self.assertEqual(self.requests, [])

    def test_normalize_and_status_map(self):
        g = api_sports.normalize_game(as_game(7, "Boston Celtics", "Denver Nuggets", "2026-11-23T00:30:00+00:00",
                                              "AOT", 120, 118), "basketball")
        self.assertEqual(g["fixture"]["id"], "as:7")
        self.assertEqual(g["fixture"]["status"]["short"], "AET")
        self.assertEqual(g["goals"], {"home": 120, "away": 118})
        self.assertEqual(api_sports.map_status("Q3"), "LIVE")
        self.assertIsNone(api_sports.normalize_game({"id": 1, "teams": {}}, "basketball"))

    def test_daily_budget_and_remote_remaining_stop_requests(self):
        cl = self.client()
        for _ in range(3):
            cl.games("basketball", "2026-11-23")
        with self.assertRaises(api_sports.BudgetExhausted):
            cl.games("basketball", "2026-11-23")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(cl.used_today("basketball"), 3)
        cl2 = self.client()
        self.remaining = "5"
        with patch.object(api_sports.settings, "api_sports_daily_budget", 50):
            cl2.games("hockey", "2026-11-23") if "hockey" in api_sports.HOSTS else None
            with self.assertRaises(api_sports.BudgetExhausted):
                cl2.games("hockey", "2026-11-23")

    def test_switch_from_espn_adopts_rows_without_duplicates(self):
        # ESPN first (no key) ...
        when = datetime.now(timezone.utc).replace(microsecond=0)
        espn_item = {"fixture": {"id": "espn:401", "date": when.isoformat(), "status": {"short": "NS", "long": "Sched"},
                                 "venue": {"name": None}, "referee": None},
                     "league": {"id": "espn:NBA", "name": "NBA"}, "country": {"name": "USA"},
                     "teams": {"home": {"id": "espn:2", "name": "Boston Celtics"}, "away": {"id": "espn:7", "name": "Denver Nuggets"}},
                     "goals": {"home": None, "away": None}}
        collector.store_fixtures([espn_item], sport_code="basketball")
        # ... then the key is set: same game from API-SPORTS, kickoff 5 min later.
        self.games = [as_game(9, "Boston Celtics", "Denver Nuggets", (when + timedelta(minutes=5)).isoformat(), "Q2", 50, 48)]
        res = multisport._collect_api_sports_days(self.client(), [when.date()], {"basketball"})
        self.assertEqual(res, {"as:basketball": 1})
        with self.eng.connect() as c:
            rows = c.execute(text("SELECT provider_match_id, status, home_score FROM matches")).all()
            teams = [r[0] for r in c.execute(text("SELECT provider_team_id FROM teams ORDER BY id")).all()]
        self.assertEqual([tuple(r) for r in rows], [("as:9", "LIVE", 50.0)])
        self.assertEqual(teams, ["as:91", "as:92"])


class KoreaLeagueTests(_ApiSportsBase):
    """Korean games from API-SPORTS while the rest of the sport stays on ESPN."""

    def kr_game(self, gid, home, away, when, short="NS", hs=None, as_=None):
        g = as_game(gid, home, away, when, short, hs, as_, league=91)
        g["league"]["name"], g["country"]["name"] = "KBL", "South Korea"
        return g

    def test_no_key_no_korea_sports(self):
        with patch.object(api_sports.settings, "api_sports_key", ""):
            self.assertEqual(api_sports.korea_sports(), set())
            self.assertEqual(multisport._collect_api_sports_days(None, [datetime.now(timezone.utc).date()],
                                                                 api_sports.korea_sports(), korea_only=True), {})
        self.assertEqual(self.requests, [])

    def test_korea_sports_skip_sports_fully_on_api_sports(self):
        # setUp puts basketball fully on API-SPORTS: its Korean games come with the full feed.
        self.assertEqual(api_sports.korea_sports(), {"baseball", "volleyball"})
        with patch.object(api_sports.settings, "api_sports_sports", "none"):
            self.assertEqual(api_sports.active_sports(), set())
            self.assertEqual(api_sports.korea_sports(), {"baseball", "basketball", "volleyball"})
            self.assertEqual(multisport.source_for("basketball"), "espn")

    def test_korea_only_stores_korean_games_without_adoption(self):
        when = datetime.now(timezone.utc).replace(microsecond=0)
        self.games = [as_game(1, "Boston Celtics", "Denver Nuggets", when.isoformat()),
                      self.kr_game(2, "Seoul SK Knights", "Busan KCC Egis", when.isoformat(), "FT", 81, 77)]
        with patch.object(api_sports.settings, "api_sports_sports", "none"):
            res = multisport._collect_api_sports_days(self.client(), [when.date()], api_sports.korea_sports() & {"basketball"},
                                                      korea_only=True)
        self.assertEqual(res, {"as-kr:basketball": 1})
        self.assertEqual(len(self.requests), 1)
        with self.eng.connect() as c:
            rows = c.execute(text("""SELECT m.provider_match_id, l.name, l.country, m.status, m.home_score
                                     FROM matches m JOIN leagues l ON l.id=m.league_id""")).all()
        self.assertEqual([tuple(r) for r in rows], [("as:2", "KBL", "South Korea", "FT", 81.0)])

    def test_empty_sport_set_means_none(self):
        self.assertEqual(multisport._collect_api_sports_days(self.client(), [datetime.now(timezone.utc).date()], set()), {})
        self.assertEqual(self.requests, [])

    def test_live_poll_refetches_korean_games(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.games = [self.kr_game(3, "Seoul SK Knights", "Busan KCC Egis", now.isoformat(), "Q2", 40, 38)]
        with patch.object(api_sports.settings, "api_sports_sports", "none"):
            multisport._collect_api_sports_days(self.client(), [now.date()], {"basketball"}, korea_only=True)
            self.requests.clear()
            res = multisport.collect_live(espn_client=object(), as_client=self.client(), now=now)
        self.assertEqual(res, {"as-kr:basketball": 1})
        self.assertEqual(len(self.requests), 1)


def sb_item(code, league, home, away, when_kst, state="pre", hs=0, as_=0, badge="예정", venue="서울월드컵경기장"):
    """One billnojay scoreboard item as /api/member/scoreboard-v30 returns it (2026-11-27 shape)."""
    return {"id": "rt-x", "sport": "soccer" if code.startswith("KLEAGUE") else "volleyball", "league": league,
            "league_code": code, "state": state, "status_badge": badge, "match_dt_full_kst": when_kst,
            "home": home, "away": away, "home_score": hs, "away_score": as_, "venue": venue,
            "ai": {"home_pct": 50, "is_sample": True}}


class KoreaScoreboardTests(unittest.TestCase):
    """Korean leagues from billnojay's scoreboard (no key)."""

    def setUp(self):
        self.eng = make_engine()
        self.requests = []
        today = datetime.now(timezone(timedelta(hours=9)))
        self.day = today.strftime("%Y.%m.%d")
        self.pages = {
            0: [sb_item("KLEAGUE", "K리그1", "FC 서울", "대전 하나 시티즌", f"{self.day} (토) 14:00 KST"),
                sb_item("VLEAGUE_W", "V-리그 여자부", "흥국생명", "GS칼텍스", f"{self.day} (토) 16:00 KST",
                        state="post", hs=3, as_=1, badge="종료"),
                {"league_code": "SOCCER_ALL", "home": "Arsenal", "away": "Chelsea",
                 "match_dt_full_kst": f"{self.day} (토) 21:00 KST"}],
        }

        def handler(request):
            off = int(request.url.params.get("date_offset", "0"))
            self.requests.append(off)
            return httpx.Response(200, json={"ok": True, "items": self.pages.get(off, [])})
        self.transport = httpx.MockTransport(handler)
        self.patches = [patch.object(collector, "engine", self.eng), patch.object(multisport, "engine", self.eng),
                        patch.object(api_sports.settings, "api_sports_key", ""),
                        patch.object(multisport.settings, "kr_scoreboard_enabled", True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def client(self):
        return kr_scoreboard.ScoreboardClient("https://billnojay.test/api/member/scoreboard-v30", transport=self.transport)

    def test_normalize_korean_only_with_stable_ids(self):
        a = kr_scoreboard.normalize(self.pages[0][0])
        b = kr_scoreboard.normalize(dict(self.pages[0][0], venue="다른 표기"))
        self.assertEqual(a[0], "football")
        f = a[1]
        self.assertEqual(f["fixture"]["id"], b[1]["fixture"]["id"])          # same game, same id
        self.assertTrue(f["fixture"]["id"].startswith("kr:"))
        self.assertEqual(f["league"]["name"], "K League 1")
        self.assertEqual(f["country"]["name"], "South Korea")
        self.assertEqual(f["teams"]["home"]["name"], "FC 서울")
        self.assertIn("kleague.com", f["teams"]["home"]["logo"])
        self.assertEqual(f["goals"], {"home": None, "away": None})            # no score before kickoff
        self.assertEqual(f["fixture"]["date"][11:16], "05:00")                # 14:00 KST = 05:00 UTC
        post = kr_scoreboard.normalize(self.pages[0][1])[1]
        self.assertEqual(post["fixture"]["status"]["short"], "FT")
        self.assertEqual(post["goals"], {"home": 3, "away": 1})
        self.assertIsNone(kr_scoreboard.normalize(self.pages[0][2]))         # not a Korean league
        self.assertIsNone(kr_scoreboard.normalize({"league_code": "KLEAGUE", "home": "A"}))

    def test_collect_stores_korean_games(self):
        res = multisport.collect_korea(self.client(), offsets=(0, 1))
        self.assertEqual(res["kr:football"], 1)
        self.assertEqual(res["kr:volleyball"], 1)
        self.assertEqual(sorted(self.requests), [0, 1])
        with self.eng.connect() as c:
            rows = c.execute(text("""SELECT s.code, l.name, l.country, ht.name, ht.logo_url, m.status, m.home_score
                                     FROM matches m JOIN sports s ON s.id=m.sport_id JOIN leagues l ON l.id=m.league_id
                                     JOIN teams ht ON ht.id=m.home_team_id ORDER BY s.code""")).all()
        self.assertEqual([(r[0], r[1], r[2], r[3], r[5]) for r in rows],
                         [("football", "K League 1", "South Korea", "FC 서울", "NS"),
                          ("volleyball", "V-League Women", "South Korea", "흥국생명", "FT")])
        self.assertIn("kovo.co.kr", rows[1][4])
        # running again updates the same rows
        multisport.collect_korea(self.client(), offsets=(0,))
        with self.eng.connect() as c:
            self.assertEqual(c.execute(text("SELECT COUNT(*) FROM matches")).scalar(), 2)

    def test_sports_served_by_api_sports_are_skipped(self):
        with patch.object(api_sports.settings, "api_sports_key", "k"), \
             patch.object(api_sports.settings, "api_sports_sports", "none"):
            # API-SPORTS brings Korean baseball/basketball/volleyball; football stays on the scoreboard
            self.assertEqual(multisport.korea_scoreboard_sports(), {"football"})
            res = multisport.collect_korea(self.client(), offsets=(0,))
        self.assertEqual(res, {"kr:football": 1})
        with patch.object(multisport.settings, "kr_scoreboard_enabled", False):
            self.assertEqual(multisport.collect_korea(self.client(), offsets=(0,)), {})

    def test_live_poll_refreshes_korean_games(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        kst = (now + timedelta(hours=9)).strftime("%Y.%m.%d (토) %H:%M KST")
        self.pages[0] = [sb_item("KLEAGUE", "K리그1", "FC 서울", "대전 하나 시티즌", kst, state="in", hs=1, badge="전반")]
        multisport.collect_korea(self.client(), offsets=(0,))
        self.requests.clear()
        res = multisport.collect_live(espn_client=object(), as_client=object(), now=now, kr_client=self.client())
        self.assertEqual(res, {"kr:football": 1})
        self.assertEqual(sorted(self.requests), [-1, 0])

    def test_korea_live_job_and_10_min_job_split_the_work(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        kst = (now + timedelta(hours=9)).strftime("%Y.%m.%d (토) %H:%M KST")
        self.pages[0] = [sb_item("KLEAGUE", "K리그1", "FC 서울", "대전 하나 시티즌", kst, state="in", hs=1, badge="전반")]
        multisport.collect_korea(self.client(), offsets=(0,))
        self.requests.clear()
        # the 2-min job reads the scoreboard for yesterday and today
        self.assertEqual(multisport.collect_korea_live(kr_client=self.client(), now=now), {"kr:football": 1})
        self.assertEqual(sorted(self.requests), [-1, 0])
        self.requests.clear()
        # the 10-min job leaves Korean games to it
        res = multisport.collect_live(espn_client=object(), as_client=object(), now=now, kr_client=self.client(),
                                      include_korea=False)
        self.assertEqual(res, {})
        self.assertEqual(self.requests, [])

    def test_korea_live_job_does_nothing_without_a_korean_game_on(self):
        later = datetime.now(timezone.utc) + timedelta(days=3)
        self.assertEqual(multisport.collect_korea_live(kr_client=self.client(), now=later), {})
        self.assertEqual(self.requests, [])


class WorkerJobTests(unittest.TestCase):
    def test_multisport_jobs_registered(self):
        from app import worker
        self.assertIsNotNone(worker.scheduler.get_job("multisport_schedule"))
        self.assertEqual(worker.scheduler.get_job("multisport_live").trigger.interval, timedelta(minutes=10))
        self.assertEqual(worker.scheduler.get_job("multisport_korea_live").trigger.interval, timedelta(minutes=2))


if __name__ == "__main__":
    unittest.main()
