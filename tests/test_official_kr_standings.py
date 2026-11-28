"""KBL, WKBL and V-League standings from the leagues' own public sites.

Fixtures are copies of the real responses on 2026-11-28:
- api.kbl.or.kr /league/rank/team (headers as sent by www.kbl.or.kr)
- wkbl.or.kr /game/ajax/ajax_team_rank.asp (season_gu=047, gun=1)
- user-api.kovo.co.kr /stat/season-list?gcode=001 (last two seasons) and
  /stat/league/team-rank?seasonCode=023&leagueCode=201
No network: _official_http_v4700 is patched with a fake router.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from app import sportiq_lounge_router as lr  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "kr_official"
WKBL_AJAX = (FIX / "wkbl_ajax_team_rank.html").read_text(encoding="utf-8")
KOVO_SEASONS = (FIX / "kovo_season_list.json").read_text(encoding="utf-8")
KOVO_RANK = (FIX / "kovo_team_rank.json").read_text(encoding="utf-8")
KBL_RANK = (FIX / "kbl_league_rank_team.json").read_text(encoding="utf-8")
WKBL_PAGE = "<script>$(function() {\n\tfnTeamRank('047', '2026-2027', '1');\n});</script>"


def fake_http(url, data=None, headers=None, timeout=4.0):
    if url == "https://api.kbl.or.kr/league/rank/team":
        # The API refuses requests without the web client's headers.
        return KBL_RANK if (headers or {}).get("Channel") == "WEB" and (headers or {}).get("TeamCode") == "XX" else ""
    if url == "https://www.wkbl.or.kr/game/team_rank.asp":
        return WKBL_PAGE
    if url == "https://www.wkbl.or.kr/game/ajax/ajax_team_rank.asp":
        return WKBL_AJAX if data == {"season_gu": "047", "gun": "1"} else ""
    if url.startswith("https://user-api.kovo.co.kr/stat/season-list"):
        return KOVO_SEASONS
    if url == "https://user-api.kovo.co.kr/stat/league/team-rank?seasonCode=023&leagueCode=201":
        return KOVO_RANK
    return ""


def poisoned_calendar(lg, team, won=False):
    """Five made-up results for team in the old hardcoded calendar's shape; live rows must ignore them."""
    hs, as_ = (90, 50) if won else (50, 90)
    if lg.startswith("VLEAGUE"):
        hs, as_ = (3, 0) if won else (0, 3)
    return {f"2026-11-{d:02d}": [[lg, "19:00", team, "상대팀", hs, as_, "가짜 체육관"]] for d in range(20, 25)}


def reset():
    lr._DOM_STANDINGS_SSR_CACHE_V4624.clear()
    lr._OFFICIAL_KR_FAIL_AT_V4700.clear()


def build(code, http=fake_http):
    reset()
    with patch.object(lr, "_official_http_v4700", side_effect=http), \
            patch.object(lr, "_http_fetch_text_v4616", return_value=""), \
            patch.object(lr, "_OFFICIAL_KOVO_WKBL_SEASON_2627_V4622", {}):
        return lr._build_domestic_bball_standings_v341(code)


class WkblOfficialTests(unittest.TestCase):
    def test_parses_official_table(self):
        res = build("WKBL")
        self.assertIs(res["is_sample"], False)
        self.assertEqual(res["source"], "WKBL 공식 기록 실시간 연동")
        rows = res["standings"]
        self.assertEqual([r["team"] for r in rows], ["삼성생명", "신한은행", "BNK썸", "우리은행", "KB스타즈", "하나은행"])
        top = rows[0]
        self.assertEqual((top["rank"], top["gp"], top["w"], top["l"], top["pts_label"]), (1, 8, 6, 2, ".750"))
        self.assertEqual(rows[1]["rank"], 1)          # tied first place kept as the league shows it
        self.assertEqual(top["streak_ko"], "1연승")
        self.assertEqual(rows[3]["gb"], "2.5")
        self.assertEqual(rows[5]["pts_label"], ".143")
        for r in rows:
            self.assertNotIn("gf", r)                  # no points-for/against in WKBL's table
            self.assertEqual(r["form"], [])
        self.assertEqual([r["zone"] for r in rows][:5], ["플레이오프"] * 4 + [""])

    def test_falls_back_to_sample_when_site_fails(self):
        res = build("WKBL", http=lambda *a, **k: "")
        self.assertIs(res["is_sample"], True)
        self.assertIn("예시", res["source"])


class KovoOfficialTests(unittest.TestCase):
    def test_men_and_women(self):
        men = build("VLEAGUE_M")
        self.assertIs(men["is_sample"], False)
        self.assertEqual(men["source"], "KOVO 공식 기록 실시간 연동")
        m = men["standings"]
        self.assertEqual(len(m), 7)
        self.assertEqual((m[0]["team"], m[0]["pts"], m[0]["w"], m[0]["l"], m[0]["set_ratio"]), ("삼성화재", 15, 5, 2, "1.889"))
        self.assertEqual((m[0]["gf"], m[0]["ga"], m[0]["gd"]), (659, 604, 55))
        self.assertEqual(m[0]["pts_label"], "15점")

        women = build("VLEAGUE_W")
        w = women["standings"]
        self.assertEqual([r["team"] for r in w][:2], ["GS칼텍스", "한국도로공사"])
        self.assertEqual(w[-1]["team"], "SOOP 수퍼스")
        self.assertEqual((w[-1]["w"], w[-1]["l"], w[-1]["pts"]), (0, 7, 1))

    def test_one_kovo_call_serves_both_genders(self):
        calls = []

        def counting(url, *a, **k):
            calls.append(url)
            return fake_http(url, *a, **k)

        reset()
        with patch.object(lr, "_official_http_v4700", side_effect=counting), \
                patch.object(lr, "_http_fetch_text_v4616", return_value=""):
            lr._build_domestic_bball_standings_v341("VLEAGUE_M")
            lr._build_domestic_bball_standings_v341("VLEAGUE_W")
        self.assertEqual(len([c for c in calls if "team-rank" in c]), 1)

    def test_failure_is_not_retried_every_request(self):
        calls = []

        def failing(url, *a, **k):
            calls.append(url)
            return ""

        reset()
        with patch.object(lr, "_official_http_v4700", side_effect=failing), \
                patch.object(lr, "_http_fetch_text_v4616", return_value=""):
            for _ in range(3):
                self.assertIs(lr._build_domestic_bball_standings_v341("VLEAGUE_W")["is_sample"], True)
        self.assertEqual(len(calls), 1)


class KblOfficialTests(unittest.TestCase):
    def test_parses_official_table(self):
        res = build("KBL")
        self.assertIs(res["is_sample"], False)
        self.assertEqual(res["source"], "KBL 공식 기록 실시간 연동")
        rows = res["standings"]
        self.assertEqual(len(rows), 10)
        top = rows[0]
        self.assertEqual((top["team"], top["gp"], top["w"], top["l"], top["pts_label"], top["gb"]), ("서울 삼성", 15, 12, 3, ".800", "-"))
        self.assertEqual((top["home_record"], top["away_record"], top["streak_ko"]), ("6-2", "6-1", "1연승"))
        # lastRecord is newest first; form is oldest -> newest, so the newest result is last.
        self.assertEqual(top["form"], ["W", "L", "W", "L", "W"])
        db = next(r for r in rows if r["team"] == "원주 DB")
        self.assertEqual((db["form"][-2:], db["streak_ko"], db["gb"]), (["L", "L"], "2연패", "4.5"))
        self.assertEqual([r["rank"] for r in rows][4:8], [5, 5, 5, 5])   # four-way tie as the league shows it
        self.assertEqual(sum(1 for r in rows if r["zone"]), 8)            # ranks 1..6 incl. the tie at 5
        for r in rows:
            self.assertNotIn("gf", r)

    def test_live_form_not_replaced_by_calendar(self):
        reset()
        with patch.object(lr, "_official_http_v4700", side_effect=fake_http),                 patch.object(lr, "_http_fetch_text_v4616", return_value=""),                 patch.object(lr, "_OFFICIAL_KOVO_WKBL_SEASON_2627_V4622", poisoned_calendar("KBL", "서울 삼성")):
            res = lr._build_domestic_bball_standings_v341("KBL")
        self.assertEqual(res["standings"][0]["form"], ["W", "L", "W", "L", "W"])

    def test_vleague_rows_ignore_calendar_form(self):
        reset()
        with patch.object(lr, "_official_http_v4700", side_effect=fake_http),                 patch.object(lr, "_http_fetch_text_v4616", return_value=""),                 patch.object(lr, "_OFFICIAL_KOVO_WKBL_SEASON_2627_V4622", poisoned_calendar("VLEAGUE_M", "한국전력", won=True)):
            res = lr._build_domestic_bball_standings_v341("VLEAGUE_M")
        kepco = next(r for r in res["standings"] if r["team"] == "한국전력")
        self.assertEqual((kepco["form"], kepco["streak_ko"]), ([], "1패"))

    def test_falls_back_to_sample_when_api_refuses(self):
        res = build("KBL", http=lambda *a, **k: '{"resultCode":"Fail","errorCode":400}')
        self.assertIs(res["is_sample"], True)


if __name__ == "__main__":
    unittest.main()
