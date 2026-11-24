"""Fixes from the 2026-11-24 review of main 06fb832.

Scratch SQLite only; no network (ESPN fetches patched).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_datacenter as dc  # noqa: E402
from app import sportiq_lounge_router as lr  # noqa: E402

SRC = Path(lr.__file__).read_text(encoding="utf-8")
FIX = Path(__file__).parent / "fixtures" / "espn_standings"


def req(xff=None, host="10.0.0.9"):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def req_h(headers, host="10.0.0.9"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


OFF_RENDER = {"RENDER": "", "SPORTIQ_TRUST_CF_CONNECTING_IP": ""}
ON_RENDER = {"RENDER": "true", "SPORTIQ_TRUST_CF_CONNECTING_IP": ""}


class ClientIpTests(unittest.TestCase):
    def test_rightmost_public_hop_not_client_supplied_leftmost(self):
        with patch.dict(os.environ, OFF_RENDER):
            self.assertEqual(lr._client_ip_v4615(req("6.6.6.6, 52.20.0.7")), "52.20.0.7")
            self.assertEqual(lr._client_ip_v4615(req("1.2.3.4, 2.2.2.2, 52.20.0.7")), "52.20.0.7")
            self.assertEqual(lr._client_ip_v4615(req("52.20.0.7")), "52.20.0.7")
            self.assertEqual(lr._client_ip_v4615(req()), "10.0.0.9")

    def test_proxy_hops_after_the_visitor_are_skipped(self):
        # If Render's balancer appends the Cloudflare edge or an internal hop,
        # every visitor must not collapse onto that one address.
        with patch.dict(os.environ, OFF_RENDER):
            self.assertEqual(lr._client_ip_v4615(req("6.6.6.6, 52.20.0.7, 172.70.1.2")), "52.20.0.7")
            self.assertEqual(lr._client_ip_v4615(req("52.20.0.7, 162.158.3.4, 10.1.2.3")), "52.20.0.7")
            self.assertEqual(lr._client_ip_v4615(req("52.20.0.7, 2606:4700::1")), "52.20.0.7")
            self.assertEqual(lr._client_ip_v4615(req("52.20.0.8:4431, 10.1.2.3")), "52.20.0.8")
            a = lr._client_ip_v4615(req("52.20.0.7, 172.70.1.2"))
            b = lr._client_ip_v4615(req("34.90.0.1, 172.70.1.2"))
            self.assertNotEqual(a, b)

    def test_cf_connecting_ip_trusted_only_on_render(self):
        h = {"cf-connecting-ip": "34.90.0.1", "x-forwarded-for": "6.6.6.6, 34.90.0.1, 172.70.1.2"}
        with patch.dict(os.environ, ON_RENDER):
            self.assertEqual(lr._client_ip_v4615(req_h(h)), "34.90.0.1")
            # Cloudflare sets it to the connecting address; with XFF alone it also matches.
            self.assertEqual(lr._client_ip_v4615(req_h({"cf-connecting-ip": "2001:4860::8"})), "2001:4860::8")
            # Junk or internal values fall back to XFF.
            self.assertEqual(lr._client_ip_v4615(req_h({"cf-connecting-ip": "nope", "x-forwarded-for": "34.90.0.1"})), "34.90.0.1")
            self.assertEqual(lr._client_ip_v4615(req_h({"cf-connecting-ip": "10.0.0.1", "x-forwarded-for": "34.90.0.1"})), "34.90.0.1")
        with patch.dict(os.environ, OFF_RENDER):
            # Off Render the header is just something a client typed.
            self.assertEqual(lr._client_ip_v4615(req_h({"cf-connecting-ip": "6.6.6.6", "x-forwarded-for": "34.90.0.1"})), "34.90.0.1")
        with patch.dict(os.environ, {"RENDER": "true", "SPORTIQ_TRUST_CF_CONNECTING_IP": "0"}):
            self.assertEqual(lr._client_ip_v4615(req_h(h)), "34.90.0.1")
            self.assertEqual(lr._client_ip_v4615(req_h({"cf-connecting-ip": "6.6.6.6", "x-forwarded-for": "34.90.0.1"})), "34.90.0.1")

    def test_all_hops_are_proxies(self):
        with patch.dict(os.environ, OFF_RENDER):
            self.assertEqual(lr._client_ip_v4615(req("10.0.0.5, 172.70.1.2")), "10.0.0.5")
            self.assertEqual(lr._client_ip_v4615(req("garbage")), "10.0.0.9")

    def test_shape_log_has_no_addresses(self):
        from app import sportiq_client_ip as ci
        ci._LOGGED[0] = False
        with patch.dict(os.environ, ON_RENDER), self.assertLogs("sportiq.client_ip", "WARNING") as cm:
            ci.client_ip(req_h({"cf-connecting-ip": "34.90.0.1", "x-forwarded-for": "34.90.0.1, 172.70.1.2"}))
            ci.client_ip(req_h({"x-forwarded-for": "52.20.0.7"}))
        self.assertEqual(len(cm.output), 1)
        line = cm.output[0]
        for addr in ("34.90.0.1", "172.70.1.2", "52.20.0.7"):
            self.assertNotIn(addr, line)
        self.assertIn("xff=[public,proxy]", line)
        self.assertIn("chosen_from=cf-connecting-ip", line)

    def test_other_ip_users_share_the_helper(self):
        root = Path(lr.__file__).parent
        main = (root / "main.py").read_text(encoding="utf-8")
        self.assertNotIn('fwd.split(",")[0]', main)
        self.assertIn("_sportiq_client_ip(request)", main)
        for name in ("sportiq_auto_analyst.py", "sportiq_issues_ranking.py"):
            self.assertIn("_sportiq_client_ip(request)", (root / name).read_text(encoding="utf-8"), name)


class VoteDedupeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app
        cls.client = TestClient(app)
        matches = cls.client.get("/api/rankings/global-matches").json()["matches"]
        cls.mid = matches[0]["id"]

    def vote(self, xff, mid=None):
        return self.client.post(f"/api/rankings/global-matches/{mid or self.mid}/vote", headers={"x-forwarded-for": xff})

    def votes(self):
        rows = self.client.get("/api/rankings/global-matches").json()["matches"]
        return next(m["votes"] for m in rows if m["id"] == self.mid)

    def test_spoofed_leftmost_values_do_not_add_votes(self):
        start = self.votes()
        first = self.vote("9.9.9.1, 52.10.0.10").json()
        self.assertFalse(first["already_voted"])
        for i in range(2, 6):
            again = self.vote(f"9.9.9.{i}, 52.10.0.10").json()      # forged leftmost, same real client
            self.assertTrue(again["already_voted"])
        self.assertEqual(self.votes(), start + 1)
        self.assertFalse(self.vote("52.10.0.11").json()["already_voted"])   # a different client still counts
        self.assertEqual(self.votes(), start + 2)

    def test_dedupe_survives_restart(self):
        self.vote("52.10.0.20")
        lr._GM_VOTE_DEDUPE_READY[0] = False       # as after a worker/web restart
        self.assertTrue(self.vote("52.10.0.20").json()["already_voted"])

    def test_failed_vote_is_not_recorded(self):
        r = self.client.post("/api/rankings/global-matches/999999/vote", headers={"x-forwarded-for": "52.10.0.30"})
        self.assertEqual(r.status_code, 404)
        key = lr._gm_vote_key(req("52.10.0.30"), 999999)
        with lr._db_session(write=False) as conn:
            row = conn.execute(f"SELECT 1 FROM {lr._GM_VOTE_DEDUPE_TABLE} WHERE vote_key = ?", (key,)).fetchone()
        self.assertIsNone(row)

    def test_table_is_in_pg_snapshot_list(self):
        from app import sportiq_auto_analyst as aa
        self.assertIn(lr._GM_VOTE_DEDUPE_TABLE, aa._PG_SNAPSHOT_TABLES)


class NoInventedAiInSearchTests(unittest.TestCase):
    def test_search_candidates_have_no_default_numbers_or_players(self):
        self.assertNotIn('"사발렌카" if is_wta else "신네르"', SRC)
        self.assertNotIn('raw_m.get("home_pct") or 52)', SRC)
        self.assertNotIn('"match_pct": 98,\n            "ai": {', SRC)
        self.assertIn('ai_real_v52 = ai.get("is_sample") is False and ai.get("home_pct") is not None', SRC)
        self.assertIn("검색 일치도 ", SRC)
        self.assertNotIn(">정확도 \" + v + \"%</span>", SRC)

    def test_other_cards_hide_example_numbers_and_odds(self):
        self.assertNotIn('(aiO.is_sample !== false ? "AI 예시 " : "AI ")', SRC)
        self.assertNotIn('(ai.is_sample !== false ? "AI 예시 " : "AI ") + (ai.home_pct || 50)', SRC)
        self.assertNotIn("var aiLblV50", SRC)
        self.assertIn('var oddsTxt = (aiRealV52 && odds.home)', SRC)
        self.assertIn('var oddsTxt = (aiRealFbV52 && odds.home)', SRC)

    def test_search_summaries_have_no_default_ai(self):
        for path in (Path(lr.__file__), Path(lr.__file__).with_name("sportiq_member_upgrade.py")):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("ai.get('home_pct', 50)", src, path.name)
            self.assertNotIn("ai_o.get('home_pct', 50)", src, path.name)
        self.assertNotIn("AI 배당 흐름", SRC)

    def test_grouped_standings_summary(self):
        self.assertIn('in_top = (int(r.get("rank") or 99) <= 2) if grouped_ext else (idx_r < 5)', SRC)
        self.assertIn("컨퍼런스 1위", SRC)
        self.assertIn('''f"{t_name} — {r.get('group')} {r.get('rank')}위" if r.get("group")''', SRC)


class StandingsFallbackTests(unittest.TestCase):
    def get(self, lg, data):
        dc._CACHE.pop(f"standings_{lg}", None)
        with patch.object(dc, "_fetch_json", return_value=data):
            return dc.get_standings(lg)

    def test_nfl_failure_shows_no_epl_table(self):
        res = self.get("NFL", None)
        self.assertTrue(res["unavailable"])
        self.assertEqual(res["standings"], [])
        self.assertEqual(res["leaders"], {"scorers": [], "assists": []})
        self.assertNotIn("리버풀", json.dumps(res, ensure_ascii=False))
        # Retried after ~5 minutes instead of being cached for 30.
        self.assertIsNone(dc._cache_get("standings_NFL", 240))

    def test_leagues_with_own_sample_still_use_it(self):
        res = self.get("NBA", None)
        self.assertTrue(res["is_sample"])
        self.assertTrue(res["standings"])

    def test_wnba_top8_overall(self):
        data = json.loads((FIX / "basketball_nba.json").read_text(encoding="utf-8"))  # same shape as WNBA
        rows = dc._us_group_rows("WNBA", data)
        self.assertEqual(sum(1 for r in rows if r["zone"] == "PO"), 8)
        po = sorted([r for r in rows if r["zone"] == "PO"], key=lambda r: -r["pct"])
        rest = [r for r in rows if r["zone"] != "PO"]
        self.assertGreaterEqual(min(r["pct"] for r in po), max(r["pct"] for r in rest))

    def test_nfl_season_sim_uses_17_games(self):
        data = json.loads((FIX / "football_nfl.json").read_text(encoding="utf-8"))
        dc._CACHE.pop("standings_NFL", None)
        with patch.object(dc, "_fetch_json", return_value=data):
            sim = dc.get_season_sim("NFL")
        rows = sim["table"]
        self.assertEqual(len(rows), 32)
        # proj_pts is projected wins for NFL: never above the 17-game season (was ~70 at 82).
        self.assertTrue(all(float(r["proj_pts"]) <= 17.0 for r in rows), max(r["proj_pts"] for r in rows))
        self.assertGreater(max(float(r["proj_pts"]) for r in rows), 10)


class H2hStringGuardTests(unittest.TestCase):
    def test_string_team_fields_do_not_crash(self):
        js = {"headToHeadGames": [{"team": "Celtics", "events": [
                  {"id": "1", "opponent": "Nuggets", "homeTeamScore": 101, "awayTeamScore": 99, "gameDate": "2025-12-01T00:00Z"}]}],
              "boxscore": {"form": [{"team": "Celtics", "events": []}]},
              "standings": {"groups": [{"standings": {"entries": [{"team": "Celtics", "stats": []}]}}]}}
        out = lr._parse_espn_summary_h2h_v4650(js, "999", "basketball", "셀틱스", "너게츠")
        self.assertIsInstance(out, tuple)


if __name__ == "__main__":
    unittest.main()
