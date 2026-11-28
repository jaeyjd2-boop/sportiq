"""KBL/WKBL/V-League standings: hardcoded tables must never pass as official.

No network: the Daum fetch (_http_fetch_text_v4616) is patched.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from app import sportiq_lounge_router as lr  # noqa: E402

LEAGUES = ("KBL", "WKBL", "VLEAGUE_M", "VLEAGUE_W")

KBL_TEAMS = ["서울 삼성", "안양 정관장", "서울 SK", "원주 DB", "부산 KCC",
             "창원 LG", "고양 소노", "대구 한국가스공사", "수원 KT", "울산 현대모비스"]
VL_M_TEAMS = ["한국전력", "현대캐피탈", "우리카드", "대한항공", "삼성화재", "OK저축은행", "KB손해보험"]


def daum_bball_html(teams):
    # Daum basketball columns: [rank, team, win_pct, gp, w, l, gb, home, away, streak]
    trs = []
    for i, t in enumerate(teams, start=1):
        w, l = 20 - i, i
        tds = [str(i), t, f"{w / (w + l):.3f}", str(w + l), str(w), str(l), "-", "5-5", "5-5", "1연승"]
        trs.append("<tr>" + "".join(f"<td>{c}</td>" for c in tds) + "</tr>")
    return "<table>" + "".join(trs) + "</table>"


def daum_vb_html(teams):
    # Daum volleyball columns: [rank, team, pts, gp, w, l, set_ratio, score_ratio, gf, ga]
    trs = []
    for i, t in enumerate(teams, start=1):
        w, l = 8 - i, i
        tds = [str(i), t, str(w * 3), str(w + l), str(w), str(l), "1.200", "1.050", str(500 + i), str(480 + i)]
        trs.append("<tr>" + "".join(f"<td>{c}</td>" for c in tds) + "</tr>")
    return "<table>" + "".join(trs) + "</table>"


def build(code, html, form_cal=None):
    lr._DOM_STANDINGS_SSR_CACHE_V4624.clear()
    with patch.object(lr, "_http_fetch_text_v4616", return_value=html), \
            patch.object(lr, "_compute_club_form_and_h2h_from_cal_v4624", return_value=(form_cal or {}, {}, None)):
        return lr._build_domestic_bball_standings_v341(code)


class FallbackIsSampleTests(unittest.TestCase):
    def test_failed_fetch_is_labelled_sample(self):
        for code in LEAGUES:
            res = build(code, "")
            self.assertIs(res["is_sample"], True, code)
            self.assertNotIn("공식", res["source"], code)
            self.assertIn("예시", res["source"], code)
            self.assertTrue(res.get("sample_note"), code)
            self.assertIs(res["leaders_is_sample"], True, code)
            self.assertTrue(res["standings"], code)

    def test_client_rendered_shell_is_sample(self):
        # Daum now serves a JS shell with no <tr> rows for these pages.
        shell = "<!DOCTYPE html><html><head><title>다음 스포츠</title></head><body><div id=app></div></body></html>"
        self.assertIs(build("KBL", shell)["is_sample"], True)

    def test_sim_carries_sample_flag(self):
        lr._DOM_STANDINGS_SSR_CACHE_V4624.clear()
        with patch.object(lr, "_http_fetch_text_v4616", return_value=""):
            sim = lr._build_domestic_bball_sim_v341("KBL", 500)
        self.assertIs(sim["is_sample"], True)


class LiveRowsNotMixedTests(unittest.TestCase):
    def test_kbl_live_rows_have_no_invented_points(self):
        res = build("KBL", daum_bball_html(KBL_TEAMS))
        self.assertIs(res["is_sample"], False)
        self.assertNotIn("예시", res["source"])
        self.assertNotIn("sample_note", res)
        self.assertEqual(len(res["standings"]), 10)
        for r in res["standings"]:
            for k in ("gf", "ga", "gd"):
                self.assertNotIn(k, r, r["team"])
            # No calendar results for this team -> empty, never the hardcoded form list.
            self.assertEqual(r["form"], [], r["team"])
        top = res["standings"][0]
        self.assertEqual((top["team"], top["w"], top["l"]), ("서울 삼성", 19, 1))
        # Leaders have no live source yet.
        self.assertIs(res["leaders_is_sample"], True)

    def test_live_form_comes_only_from_calendar(self):
        res = build("KBL", daum_bball_html(KBL_TEAMS), form_cal={"서울 SK": ["L", "W", "L", "W", "W", "L"]})
        by_tm = {r["team"]: r for r in res["standings"]}
        self.assertEqual(by_tm["서울 SK"]["form"], ["W", "L", "W", "W", "L"])
        self.assertEqual(by_tm["서울 삼성"]["form"], [])

    def test_vleague_live_rows_keep_daum_values(self):
        res = build("VLEAGUE_M", daum_vb_html(VL_M_TEAMS))
        self.assertIs(res["is_sample"], False)
        rows = res["standings"]
        self.assertEqual([r["team"] for r in rows], VL_M_TEAMS)
        # Values exactly as Daum gave them; no hardcoded result patched in.
        kb = rows[-1]
        self.assertEqual((kb["gp"], kb["w"], kb["l"], kb["gf"], kb["ga"]), (8, 1, 7, 507, 487))
        for r in rows:
            self.assertEqual(r["form"], [], r["team"])

    def test_sim_runs_without_goal_difference(self):
        lr._DOM_STANDINGS_SSR_CACHE_V4624.clear()
        with patch.object(lr, "_http_fetch_text_v4616", return_value=daum_bball_html(KBL_TEAMS)):
            sim = lr._build_domestic_bball_sim_v341("KBL", 500)
        self.assertIs(sim["is_sample"], False)
        self.assertEqual(len(sim["table"]), 10)


if __name__ == "__main__":
    unittest.main()
