"""No invented track records or odds alerts; hall-of-fame script parses; H2H injuries exact.

Scratch SQLite only (SPORTIQ_LOUNGE_DB points to a temp file); no network.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from app import sportiq_autonomous_sec as au  # noqa: E402
from app import sportiq_datacenter as dc  # noqa: E402
from app import sportiq_pro_lab as pl  # noqa: E402

APP = Path(au.__file__).parent

PRE = {"event_id": "e-pre-1", "match_key": "espn_e-pre-1", "match_date": "2026-11-24", "sport": "basketball",
       "league": "NBA", "league_color": "#c9082a", "matchup": "A vs B", "home_team": "A", "away_team": "B",
       "schedule_venue": "11.24 09:00 · Arena"}
FINAL = {"event_id": "e-fin-1", "match_date": "2026-11-22", "sport": "basketball", "league": "NBA",
         "matchup": "C vs D", "home_team": "C", "away_team": "D", "home_score": 101, "away_score": 99,
         "result_score": "101-99"}


def count(table):
    conn = sqlite3.connect(au.DB_PATH)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


class GraderTests(unittest.TestCase):
    def test_hash_picks_are_not_recorded_or_graded(self):
        au._init_autonomous_db()
        before = (count("live_prediction_ledger"), count("ai_scorecard_records"))
        with patch.object(au, "_parse_espn_events", return_value=([PRE], [FINAL])), \
                patch.object(au, "_backup_pg"), \
                patch.object(au._auto_analyst, "run_daily_00kst_analysis", create=True), \
                patch.object(au._auto_analyst, "run_auto_analysis", create=True):
            res = au.run_top20_auto_rotation_and_grading()
        self.assertEqual((res["recorded_pre"], res["graded_final"]), (0, 0))
        self.assertEqual((count("live_prediction_ledger"), count("ai_scorecard_records")), before)
        self.assertEqual(au._PIPELINE_STATUS["scorecard_auto_grader"]["status"], "disabled_no_real_model")

    def test_scorecard_outputs_are_marked_as_examples(self):
        sc = pl.get_ai_scorecard()
        self.assertFalse(sc["verified_records_available"])
        self.assertEqual(sc["real_total_matches"], 0)
        self.assertTrue(all(r["is_sample"] for r in sc["records"]))
        self.assertIn("실제 실적이 아닙니다", sc["sample_note"])

        csv_text = au.download_scorecard_csv().body.decode("utf-8-sig")
        lines = csv_text.splitlines()
        self.assertTrue(lines[0].startswith("# "))
        self.assertTrue(lines[1].endswith(",is_sample"))
        for row in lines[2:]:
            self.assertIn("[예시·실적 아님]", row)
            self.assertTrue(row.endswith(",1"))

    def test_ask_ai_gives_no_hit_rate(self):
        with patch.object(au, "_compute_value_odds_list", return_value=[]):
            ans = au.ask_sportiq_scoby(au.AskAIRequest(question="AI 적중률 성적"))["answer"]
        self.assertNotRegex(ans, r"\d+(\.\d+)?%")
        self.assertIn("실제 적중 실적이 아닙니다", ans)


class MyAlertsTests(unittest.TestCase):
    def test_no_sample_odds_or_sample_injury_alerts(self):
        live = {"matches": [{"id": "EPL-1", "sport": "soccer", "league": "EPL", "home": "Tottenham",
                             "away": "Liverpool", "home_score": 1, "away_score": 0, "state": "in"}],
                "score_alerts": [], "odds_alerts": [{"id": "x", "sport": "soccer", "league": "EPL", "text": "fake"}]}
        sample_inj = {"items": [{"team": "Tottenham", "player": "P", "reason": "r", "severity": "s"}], "is_sample": True}
        with patch.object(dc, "get_live_scores", return_value=live), \
                patch.object(dc, "get_injuries", return_value=sample_inj), \
                patch.object(dc, "_cache_get", return_value=None), \
                patch.object(dc, "_build_dropping_odds", return_value={"items": [{"sport": "soccer", "league": "EPL", "home": "Tottenham", "away": "Liverpool", "drop_pct": 12}]}):
            res = dc.get_my_alerts(teams="토트넘,tottenham", sports="soccer", category="all", user_key="guest")
        types = {x["type"] for x in res["items"]}
        self.assertNotIn("odds", types)
        self.assertNotIn("injury", types)
        self.assertIn("score", types)
        self.assertEqual(res["counts"]["odds"], 0)
        self.assertTrue(res["odds_alerts_excluded"])


class FrontEndTests(unittest.TestCase):
    def test_community_inline_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        html = (APP / "sportiq_community.html").read_text(encoding="utf-8")
        self.assertNotIn("h+++'<div", html)

        # Tokenize script elements like a browser does (a malformed
        # `<script src="..."</script>` swallowed the next inline block).
        from html.parser import HTMLParser

        class Scripts(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.inline, self.src_with_body, self._cur = [], [], None

            def handle_starttag(self, tag, attrs):
                if tag == "script":
                    self._cur = {"src": dict(attrs).get("src"), "body": []}

            def handle_data(self, data):
                if self._cur is not None:
                    self._cur["body"].append(data)

            def handle_endtag(self, tag):
                if tag == "script" and self._cur is not None:
                    body = "".join(self._cur["body"])
                    if self._cur["src"]:
                        if body.strip():
                            self.src_with_body.append(self._cur["src"])
                    else:
                        self.inline.append(body)
                    self._cur = None

        parser = Scripts()
        parser.feed(html)
        self.assertEqual(parser.src_with_body, [], "a src script swallowed inline code")
        scripts = parser.inline
        self.assertTrue(any("hofSort" in sc for sc in scripts))
        with tempfile.TemporaryDirectory() as td:
            for i, sc in enumerate(scripts):
                f = Path(td) / f"s{i}.js"
                f.write_text(sc, encoding="utf-8")
                r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"script {i}: {r.stderr[-300:]}")

    def test_scorecard_modal_shows_example_notice(self):
        html = (APP / "sportiq_community.html").read_text(encoding="utf-8")
        self.assertIn("d.verified_records_available === false", html)
        self.assertIn("예시 기록 CSV 다운로드", html)

    def test_h2h_injuries_exact_team_match(self):
        src = (APP / "sportiq_lounge_router.py").read_text(encoding="utf-8")
        self.assertIn("function injSideV51(x)", src)
        self.assertIn("injData && !injData.is_sample && Array.isArray(injData.items)", src)
        self.assertNotIn("tmKo.indexOf(hClean) !== -1 || hClean.indexOf(tmKo) !== -1", src)


if __name__ == "__main__":
    unittest.main()
